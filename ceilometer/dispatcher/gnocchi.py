#
# Copyright 2014 eNovance
#
# Authors: Julien Danjou <julien@danjou.info>
#          Mehdi Abaakouk <mehdi.abaakouk@enovance.com>
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
import fnmatch
import itertools
import json
import operator
import os
import threading

import jsonpath_rw
from oslo_config import cfg
from oslo_log import log
import requests
import six
import yaml

from ceilometer import dispatcher
from ceilometer.i18n import _, _LE
from ceilometer import keystone_client

LOG = log.getLogger(__name__)

dispatcher_opts = [
    cfg.BoolOpt('filter_service_activity',
                default=True,
                help='Filter out samples generated by Gnocchi '
                'service activity'),
    cfg.StrOpt('filter_project',
               default='gnocchi',
               help='Gnocchi project used to filter out samples '
               'generated by Gnocchi service activity'),
    cfg.StrOpt('url',
               default="http://localhost:8041",
               help='URL to Gnocchi.'),
    cfg.StrOpt('archive_policy',
               default="low",
               help='The archive policy to use when the dispatcher '
               'create a new metric.'),
    cfg.StrOpt('archive_policy_file',
               default='gnocchi_archive_policy_map.yaml',
               deprecated_for_removal=True,
               help=_('The Yaml file that defines per metric archive '
                      'policies.')),
    cfg.StrOpt('resources_definition_file',
               default='gnocchi_resources.yaml',
               help=_('The Yaml file that defines mapping between samples '
                      'and gnocchi resources/metrics')),
]

cfg.CONF.register_opts(dispatcher_opts, group="dispatcher_gnocchi")


class UnexpectedWorkflowError(Exception):
    pass


class NoSuchMetric(Exception):
    pass


class MetricAlreadyExists(Exception):
    pass


class NoSuchResource(Exception):
    pass


class ResourceAlreadyExists(Exception):
    pass


def log_and_ignore_unexpected_workflow_error(func):
    def log_and_ignore(self, *args, **kwargs):
        try:
            func(self, *args, **kwargs)
        except requests.ConnectionError as e:
            with self._gnocchi_api_lock:
                self._gnocchi_api = None
            LOG.warn("Connection error, reconnecting...")
        except UnexpectedWorkflowError as e:
            LOG.error(six.text_type(e))
    return log_and_ignore


class LegacyArchivePolicyDefinition(object):
    def __init__(self, definition_cfg):
        self.cfg = definition_cfg
        if self.cfg is None:
            LOG.debug(_("No archive policy file found!"
                      " Using default config."))

    def get(self, metric_name):
        if self.cfg is not None:
            for metric, policy in self.cfg.items():
                # Support wild cards such as disk.*
                if fnmatch.fnmatch(metric_name, metric):
                    return policy


class ResourcesDefinitionException(Exception):
    def __init__(self, message, definition_cfg):
        super(ResourcesDefinitionException, self).__init__(message)
        self.definition_cfg = definition_cfg

    def __str__(self):
        return '%s %s: %s' % (self.__class__.__name__,
                              self.definition_cfg, self.message)


class ResourcesDefinition(object):

    MANDATORY_FIELDS = {'resource_type': six.string_types,
                        'metrics': list}

    def __init__(self, definition_cfg, default_archive_policy,
                 legacy_archive_policy_defintion):
        self._default_archive_policy = default_archive_policy
        self._legacy_archive_policy_defintion = legacy_archive_policy_defintion
        self.cfg = definition_cfg
        self._validate()

    def match(self, metric_name):
        for t in self.cfg['metrics']:
            if fnmatch.fnmatch(metric_name, t):
                return True
        return False

    def attributes(self, sample):
        attrs = {}
        for attribute_info in self.cfg.get('attributes', []):
            for attr, field in attribute_info.items():
                value = self._parse_field(field, sample)
                if value is not None:
                    attrs[attr] = value
        return attrs

    def metrics(self):
        metrics = {}
        for t in self.cfg['metrics']:
            archive_policy = self.cfg.get(
                'archive_policy',
                self._legacy_archive_policy_defintion.get(t))
            metrics[t] = dict(archive_policy_name=archive_policy or
                              self._default_archive_policy)
        return metrics

    def _parse_field(self, field, sample):
        # TODO(sileht): share this with
        # https://review.openstack.org/#/c/197633/
        if not field:
            return
        if isinstance(field, six.integer_types):
            return field
        try:
            parts = jsonpath_rw.parse(field)
        except Exception as e:
            raise ResourcesDefinitionException(
                _LE("Parse error in JSONPath specification "
                    "'%(jsonpath)s': %(err)s")
                % dict(jsonpath=field, err=e), self.cfg)
        values = [match.value for match in parts.find(sample)
                  if match.value is not None]
        if values:
            return values[0]

    def _validate(self):
        for field, field_type in self.MANDATORY_FIELDS.items():
            if field not in self.cfg:
                raise ResourcesDefinitionException(
                    _LE("Required field %s not specified") % field, self.cfg)
            if not isinstance(self.cfg[field], field_type):
                raise ResourcesDefinitionException(
                    _LE("Required field %(field)s should be a %(type)s") %
                    {'field': field, 'type': field_type}, self.cfg)


class GnocchiDispatcher(dispatcher.Base):
    def __init__(self, conf):
        super(GnocchiDispatcher, self).__init__(conf)
        self.conf = conf
        self.filter_service_activity = (
            conf.dispatcher_gnocchi.filter_service_activity)
        self._ks_client = keystone_client.get_client()
        self.gnocchi_url = conf.dispatcher_gnocchi.url
        self.gnocchi_archive_policy_data = self._load_archive_policy(conf)
        self.resources_definition = self._load_resources_definitions(conf)

        self._gnocchi_project_id = None
        self._gnocchi_project_id_lock = threading.Lock()
        self._gnocchi_api = None
        self._gnocchi_api_lock = threading.Lock()

    def _get_headers(self, content_type="application/json"):
        return {
            'Content-Type': content_type,
            'X-Auth-Token': self._ks_client.auth_token,
        }

    # TODO(sileht): Share yaml loading with
    # event converter and declarative notification

    @staticmethod
    def _get_config_file(conf, config_file):
        if not os.path.exists(config_file):
            config_file = cfg.CONF.find_file(config_file)
        return config_file

    @classmethod
    def _load_resources_definitions(cls, conf):
        res_def_file = cls._get_config_file(
            conf, conf.dispatcher_gnocchi.resources_definition_file)
        data = {}
        if res_def_file is not None:
            with open(res_def_file) as data_file:
                try:
                    data = yaml.safe_load(data_file)
                except ValueError:
                    data = {}

        legacy_archive_policies = cls._load_archive_policy(conf)
        return [ResourcesDefinition(r, conf.dispatcher_gnocchi.archive_policy,
                                    legacy_archive_policies)
                for r in data.get('resources', [])]

    @classmethod
    def _load_archive_policy(cls, conf):
        policy_config_file = cls._get_config_file(
            conf, conf.dispatcher_gnocchi.archive_policy_file)
        data = {}
        if policy_config_file is not None:
            with open(policy_config_file) as data_file:
                try:
                    data = yaml.safe_load(data_file)
                except ValueError:
                    data = {}
        return LegacyArchivePolicyDefinition(data)

    @property
    def gnocchi_project_id(self):
        if self._gnocchi_project_id is not None:
            return self._gnocchi_project_id
        with self._gnocchi_project_id_lock:
            if self._gnocchi_project_id is None:
                try:
                    project = self._ks_client.tenants.find(
                        name=self.conf.dispatcher_gnocchi.filter_project)
                except Exception:
                    LOG.exception('fail to retreive user of Gnocchi service')
                    raise
                self._gnocchi_project_id = project.id
                LOG.debug("gnocchi project found: %s" %
                          self.gnocchi_project_id)
            return self._gnocchi_project_id

    @property
    def gnocchi_api(self):
        """return a working requests session object"""
        if self._gnocchi_api is not None:
            return self._gnocchi_api

        with self._gnocchi_api_lock:
            if self._gnocchi_api is None:
                self._gnocchi_api = requests.session()
                # NOTE(sileht): wait when the pool is empty
                # instead of raising errors.
                adapter = requests.adapters.HTTPAdapter(pool_block=True)
                self._gnocchi_api.mount("http://", adapter)
                self._gnocchi_api.mount("https://", adapter)

            return self._gnocchi_api

    def _is_swift_account_sample(self, sample):
        return bool([rd for rd in self.resources_definition
                     if rd.cfg['resource_type'] == 'swift_account'
                     and rd.match(sample['counter_name'])])

    def _is_gnocchi_activity(self, sample):
        return (self.filter_service_activity and (
            # avoid anything from the user used by gnocchi
            sample['project_id'] == self.gnocchi_project_id or
            # avoid anything in the swift account used by gnocchi
            (sample['resource_id'] == self.gnocchi_project_id and
             self._is_swift_account_sample(sample))
        ))

    def _get_resource_definition(self, metric_name):
        for rd in self.resources_definition:
            if rd.match(metric_name):
                return rd

    def record_metering_data(self, data):
        # NOTE(sileht): skip sample generated by gnocchi itself
        data = [s for s in data if not self._is_gnocchi_activity(s)]

        # FIXME(sileht): This method bulk the processing of samples
        # grouped by resource_id and metric_name but this is not
        # efficient yet because the data received here doesn't often
        # contains a lot of different kind of samples
        # So perhaps the next step will be to pool the received data from
        # message bus.
        data.sort(key=lambda s: (s['resource_id'], s['counter_name']))

        resource_grouped_samples = itertools.groupby(
            data, key=operator.itemgetter('resource_id'))

        for resource_id, samples_of_resource in resource_grouped_samples:
            resource_need_to_be_updated = True

            metric_grouped_samples = itertools.groupby(
                list(samples_of_resource),
                key=operator.itemgetter('counter_name'))
            for metric_name, samples in metric_grouped_samples:
                samples = list(samples)
                rd = self._get_resource_definition(metric_name)
                if rd:
                    self._process_samples(rd, resource_id, metric_name,
                                          samples,
                                          resource_need_to_be_updated)
                else:
                    LOG.warn("metric %s is not handled by gnocchi" %
                             metric_name)

                # FIXME(sileht): Does it reasonable to skip the resource
                # update here ? Does differents kind of counter_name
                # can have different metadata set ?
                # (ie: one have only flavor_id, and an other one have only
                # image_ref ?)
                #
                # resource_need_to_be_updated = False

    @log_and_ignore_unexpected_workflow_error
    def _process_samples(self, resource_def, resource_id, metric_name, samples,
                         resource_need_to_be_updated):
        resource_type = resource_def.cfg['resource_type']
        measure_attributes = [{'timestamp': sample['timestamp'],
                               'value': sample['counter_volume']}
                              for sample in samples]

        try:
            self._post_measure(resource_type, resource_id, metric_name,
                               measure_attributes)
        except NoSuchMetric:
            # NOTE(sileht): we try first to create the resource, because
            # they more chance that the resource doesn't exists than the metric
            # is missing, the should be reduce the number of resource API call
            resource_attributes = self._get_resource_attributes(
                resource_def, resource_id, metric_name, samples)
            try:
                self._create_resource(resource_type, resource_id,
                                      resource_attributes)
            except ResourceAlreadyExists:
                try:
                    archive_policy = (resource_def.metrics()[metric_name])
                    self._create_metric(resource_type, resource_id,
                                        metric_name, archive_policy)
                except MetricAlreadyExists:
                    # NOTE(sileht): Just ignore the metric have been created in
                    # the meantime.
                    pass
            else:
                # No need to update it we just created it
                # with everything we need
                resource_need_to_be_updated = False

            # NOTE(sileht): we retry to post the measure but if it fail we
            # don't catch the exception to just log it and continue to process
            # other samples
            self._post_measure(resource_type, resource_id, metric_name,
                               measure_attributes)

        if resource_need_to_be_updated:
            resource_attributes = self._get_resource_attributes(
                resource_def, resource_id, metric_name, samples,
                for_update=True)
            if resource_attributes:
                self._update_resource(resource_type, resource_id,
                                      resource_attributes)

    def _get_resource_attributes(self, resource_def, resource_id, metric_name,
                                 samples, for_update=False):
        # FIXME(sileht): Should I merge attibutes of all samples ?
        # Or keep only the last one is sufficient ?
        attributes = resource_def.attributes(samples[-1])
        if not for_update:
            attributes["id"] = resource_id
            attributes["user_id"] = samples[-1]['user_id']
            attributes["project_id"] = samples[-1]['project_id']
            attributes["metrics"] = resource_def.metrics()
        return attributes

    def _post_measure(self, resource_type, resource_id, metric_name,
                      measure_attributes):
        r = self.gnocchi_api.post("%s/v1/resource/%s/%s/metric/%s/measures"
                                  % (self.gnocchi_url, resource_type,
                                     resource_id, metric_name),
                                  headers=self._get_headers(),
                                  data=json.dumps(measure_attributes))
        if r.status_code == 404:
            LOG.debug(_("The metric %(metric_name)s of "
                        "resource %(resource_id)s doesn't exists: "
                        "%(status_code)d"),
                      {'metric_name': metric_name,
                       'resource_id': resource_id,
                       'status_code': r.status_code})
            raise NoSuchMetric
        elif r.status_code // 100 != 2:
            raise UnexpectedWorkflowError(
                _("Fail to post measure on metric %(metric_name)s of "
                  "resource %(resource_id)s with status: "
                  "%(status_code)d: %(msg)s") %
                {'metric_name': metric_name,
                 'resource_id': resource_id,
                 'status_code': r.status_code,
                 'msg': r.text})
        else:
            LOG.debug("Measure posted on metric %s of resource %s",
                      metric_name, resource_id)

    def _create_resource(self, resource_type, resource_id,
                         resource_attributes):
        r = self.gnocchi_api.post("%s/v1/resource/%s"
                                  % (self.gnocchi_url, resource_type),
                                  headers=self._get_headers(),
                                  data=json.dumps(resource_attributes))
        if r.status_code == 409:
            LOG.debug("Resource %s already exists", resource_id)
            raise ResourceAlreadyExists

        elif r.status_code // 100 != 2:
            raise UnexpectedWorkflowError(
                _("Resource %(resource_id)s creation failed with "
                  "status: %(status_code)d: %(msg)s") %
                {'resource_id': resource_id,
                 'status_code': r.status_code,
                 'msg': r.text})
        else:
            LOG.debug("Resource %s created", resource_id)

    def _update_resource(self, resource_type, resource_id,
                         resource_attributes):
        r = self.gnocchi_api.patch(
            "%s/v1/resource/%s/%s"
            % (self.gnocchi_url, resource_type, resource_id),
            headers=self._get_headers(),
            data=json.dumps(resource_attributes))

        if r.status_code // 100 != 2:
            raise UnexpectedWorkflowError(
                _("Resource %(resource_id)s update failed with "
                  "status: %(status_code)d: %(msg)s") %
                {'resource_id': resource_id,
                 'status_code': r.status_code,
                 'msg': r.text})
        else:
            LOG.debug("Resource %s updated", resource_id)

    def _create_metric(self, resource_type, resource_id, metric_name,
                       archive_policy):
        params = {metric_name: archive_policy}
        r = self.gnocchi_api.post("%s/v1/resource/%s/%s/metric"
                                  % (self.gnocchi_url, resource_type,
                                     resource_id),
                                  headers=self._get_headers(),
                                  data=json.dumps(params))
        if r.status_code == 409:
            LOG.debug("Metric %s of resource %s already exists",
                      metric_name, resource_id)
            raise MetricAlreadyExists

        elif r.status_code // 100 != 2:
            raise UnexpectedWorkflowError(
                _("Fail to create metric %(metric_name)s of "
                  "resource %(resource_id)s with status: "
                  "%(status_code)d: %(msg)s") %
                {'metric_name': metric_name,
                 'resource_id': resource_id,
                 'status_code': r.status_code,
                 'msg': r.text})
        else:
            LOG.debug("Metric %s of resource %s created",
                      metric_name, resource_id)

    @staticmethod
    def record_events(events):
        raise NotImplementedError
