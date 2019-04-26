# (C) Datadog, Inc. 2019
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
from six import iteritems
from requests.auth import HTTPBasicAuth
import requests


from datadog_checks.base import AgentCheck
from datadog_checks.base.errors import CheckException
from . import common

# Tag templates
CLUSTER_TAG_TEMPLATE = "ambari_cluster:{}"
HOST_TAG = "ambari_host:"
SERVICE_TAG = "ambari_service:"
COMPONENT_TAG = "ambari_component:"

# URL queries
COMPONENT_METRICS_QUERY = "/components?fields=metrics"
SERVICE_INFO_QUERY = "?fields=ServiceInfo"

# Response fields
METRICS_FIELD = "metrics"


class AmbariCheck(AgentCheck):
    def __init__(self, *args, **kwargs):
        super(AmbariCheck, self).__init__(args, kwargs)
        self.hosts = []
        self.clusters = []
        self.services = []

    def check(self, instance):
        server = instance.get("url", "")
        port = str(instance.get("port", ""))
        path = str(instance.get("path", ""))
        base_tags = instance.get("tags", [])
        whitelisted_services = instance.get("services", [])
        authentication = {'username': instance.get("username", ""),
                          'password': instance.get("password", "")
                          }
        whitelisted_metrics = [str(h) for h in instance.get("metric_headers", [])]

        base_url = "{}:{}{}".format(server, port, path)
        clusters = self.get_clusters(base_url, authentication)
        self.get_host_metrics(base_url, authentication, clusters, base_tags)
        self.get_service_metrics(base_url, authentication, clusters, whitelisted_services, whitelisted_metrics, base_tags)

    def get_clusters(self, base_url, authentication):
        clusters_endpoint = common.CLUSTERS_URL.format(base_url=base_url)

        resp = self.make_request(clusters_endpoint, authentication)
        if resp is None:
            self.submit_service_checks("can_connect", self.CRITICAL, ["url:{}".format(base_url)])
            raise CheckException("Couldn't connect to URL: {}. Please verify the address is reachable".format(clusters_endpoint))

        self.submit_service_checks("can_connect", self.OK, ["url:{}".format(base_url)])
        return [cluster.get('Clusters').get('cluster_name') for cluster in resp.get('items')]

    def get_host_metrics(self, base_url, authentication, clusters, base_tags):
        for cluster in clusters:
            cluster_tag = CLUSTER_TAG_TEMPLATE.format(cluster)
            hosts_list = self.get_hosts(base_url, authentication, cluster)

            for host in hosts_list:
                if host.get(METRICS_FIELD) is None:
                    self.log.warning("No metrics received for host {}".format(host.get('Hosts').get('host_name')))
                    continue

                metrics = self.flatten_host_metrics(host.get(METRICS_FIELD))
                for metric_name, value in iteritems(metrics):
                    host_tag = HOST_TAG + host.get('Hosts').get('host_name')
                    metric_tags = base_tags + [cluster_tag, host_tag]
                    if isinstance(value, float):
                        self.submit_gauge(metric_name, value, metric_tags)
                    else:
                        self.log.warning("Encountered non float metric {}:{}".format(metric_name, value))

    def get_hosts(self, base_url, authentication, cluster):
        hosts_endpoint = common.HOST_METRICS_URL.format(
            base_url=base_url,
            cluster_name=cluster
        )
        resp = self.make_request(hosts_endpoint, authentication)

        return resp.get('items')

    def get_service_metrics(self, base_url, authentication, clusters, whitelisted_services,
                            whitelisted_metrics, base_tags):
        for cluster in clusters:
            tags = base_tags + [CLUSTER_TAG_TEMPLATE.format(cluster)]
            for service, components in iteritems(whitelisted_services):
                service_tags = tags + [SERVICE_TAG + service.lower()]
                self.get_component_metrics(base_url, authentication, cluster, service,
                                           service_tags, [c.upper() for c in components], whitelisted_metrics)
                self.get_service_checks(base_url, authentication, cluster, service, service_tags)

    def get_service_checks_info(self, base_url, authentication, cluster, service, service_tags):
        service_check_endpoint = common.create_endpoint(base_url, cluster, service, SERVICE_INFO_QUERY)
        service_info = []
        service_resp = self.make_request(service_check_endpoint, authentication)
        if service_resp is None:
            service_info.append({'state': self.CRITICAL, 'tags': service_tags})
            self.log.warning("No response received for service {}".format(service))
        else:
            state = service_resp.get('ServiceInfo').get('state')
            service_info.append({'state': common.STATUS[state], 'tags': service_tags})
        return service_info

    def get_service_checks(self, base_url, authentication, cluster, service, service_tags):
        service_info = self.get_service_checks_info(base_url, authentication, cluster, service, service_tags)
        for info in service_info:
            self.submit_service_checks("state", info['state'], info['tags'])

    def get_component_metrics(self, base_url, authentication, cluster, service,
                              base_tags, component_whitelist, metric_whitelist):
        component_metrics_endpoint = common.create_endpoint(base_url, cluster, service, COMPONENT_METRICS_QUERY)
        resp = self.make_request(component_metrics_endpoint, authentication)

        if resp is None:
            self.log.warning("No components found for service {}.".format(service))
            return

        for component in resp.get('items'):
            component_name = component.get('ServiceComponentInfo').get('component_name')

            if component_name not in component_whitelist:
                self.log.warning('{} not found in {}:{}'.format(component_name, cluster, service))
                continue
            if component.get(METRICS_FIELD) is None:
                self.log.warning(
                    "No metrics found for component {} for service {}"
                        .format(component_name, service)
                )
                continue

            for header in metric_whitelist:
                if header not in component.get(METRICS_FIELD):
                    self.log.warning(
                        "No {} metrics found for component {} for service {}"
                            .format(header, component_name, service)
                    )
                    continue

                metrics = self.flatten_service_metrics(component.get(METRICS_FIELD)[header], header)
                component_tag = COMPONENT_TAG + component_name.lower()
                for metric_name, value in iteritems(metrics):
                    metric_tags = base_tags + [component_tag]
                    if isinstance(value, float):
                        self.submit_gauge(metric_name, value, metric_tags)
                    else:
                        self.log.warning("Expected a float for {}, received {}".format(metric_name, value))

    @staticmethod
    def flatten_service_metrics(metric_dict, prefix):
        flat_metrics = {}
        for raw_metric_name, metric_value in iteritems(metric_dict):
            if isinstance(metric_value, dict):
                flat_metrics.update(AmbariCheck.flatten_service_metrics(metric_value, prefix))
            else:
                metric_name = '{}.{}'.format(prefix, raw_metric_name) if prefix else raw_metric_name
                flat_metrics[metric_name] = metric_value
        return flat_metrics

    @staticmethod
    def flatten_host_metrics(metric_dict, prefix=""):
        flat_metrics = {}
        for raw_metric_name, metric_value in iteritems(metric_dict):
            metric_name = '{}.{}'.format(prefix, raw_metric_name) if prefix else raw_metric_name
            if raw_metric_name == "boottime":
                flat_metrics["boottime"] = metric_value
            elif isinstance(metric_value, dict):
                flat_metrics.update(AmbariCheck.flatten_host_metrics(metric_value, metric_name))
            else:
                flat_metrics[metric_name] = metric_value
        return flat_metrics

    def make_request(self, url, auth):
        try:
            resp = self.http.get(url,
                                 auth=HTTPBasicAuth(auth.get('username'), auth.get('password')),
                                 verify=False)  # In case Ambari is under uncertified https
            return resp.json()
        except (requests.exceptions.HTTPError, requests.exceptions.ConnectionError) as e:
            self.warning(
                "Couldn't connect to URL: {} with exception: {}. Please verify the address is reachable"
                .format(url, e))
        except requests.exceptions.Timeout:
            self.warning("Connection timeout when connecting to {}".format(url))

    def submit_gauge(self, name, value, tags):
        self.gauge('{}.{}'.format(common.METRIC_PREFIX, name), value, tags)

    def submit_service_checks(self, name, value, tags):
        self.service_check('{}.{}'.format(common.METRIC_PREFIX, name), value, tags)