"""
Unit tests for the Elasticsearch DXL client (no broker required): the DXL
client is replaced by a fake that records the requests and returns canned
responses.
"""

import json
import unittest

from elasticsearch.exceptions import NotFoundError, TransportError, \
    ConnectionError as EsConnectionError
from dxlclient.message import ErrorResponse, Request, Response
from dxlelasticsearchclient import ElasticsearchClient


class FakeDxlClient(object):
    """Fake DxlClient answering every request with the configured payload"""

    def __init__(self, response_dict=None, error_dict=None):
        self.response_dict = response_dict if response_dict is not None \
            else {"result": "ok"}
        self.error_dict = error_dict
        self.requests = []

    def sync_request(self, request, timeout=None): # pylint: disable=unused-argument
        self.requests.append(request)
        if self.error_dict is not None:
            response = ErrorResponse(request, error_code=1,
                                     error_message="service error")
            response.payload = json.dumps(self.error_dict).encode("utf-8")
            return response
        response = Response(request)
        response.payload = json.dumps(self.response_dict).encode("utf-8")
        return response

    @property
    def last_request(self):
        return self.requests[-1]

    @property
    def last_payload(self):
        return json.loads(self.last_request.payload.decode("utf-8"))


def _es_error(class_name, data=None):
    error = {"module": "elasticsearch.exceptions", "class": class_name}
    if data is not None:
        error["data"] = data
    return error


class ElasticsearchClientTest(unittest.TestCase):

    def test_get_request(self):
        dxl_client = FakeDxlClient({"_source": {"a": 1}})
        client = ElasticsearchClient(dxl_client)
        result = client.get("idx", "doc", "42", _source_include="a")
        self.assertEqual({"_source": {"a": 1}}, result)
        self.assertIsInstance(dxl_client.last_request, Request)
        self.assertEqual(
            "/opendxl-elasticsearch/service/elasticsearch-api/get",
            dxl_client.last_request.destination_topic)
        self.assertEqual(
            {"index": "idx", "doc_type": "doc", "id": "42",
             "_source_include": "a"},
            dxl_client.last_payload)

    def test_service_unique_id_in_topic(self):
        dxl_client = FakeDxlClient()
        client = ElasticsearchClient(dxl_client,
                                     elasticsearch_service_unique_id="es1")
        client.delete("idx", "doc", "42")
        self.assertEqual(
            "/opendxl-elasticsearch/service/elasticsearch-api/es1/delete",
            dxl_client.last_request.destination_topic)

    def test_index_and_update_payloads(self):
        dxl_client = FakeDxlClient()
        client = ElasticsearchClient(dxl_client)
        client.index("idx", "doc", {"name": "x"}, refresh=True)
        self.assertEqual(
            "/opendxl-elasticsearch/service/elasticsearch-api/index",
            dxl_client.last_request.destination_topic)
        self.assertEqual(
            {"index": "idx", "doc_type": "doc", "body": {"name": "x"},
             "id": None, "refresh": True},
            dxl_client.last_payload)
        client.update("idx", "doc", "42", {"doc": {"name": "y"}})
        self.assertEqual(
            "/opendxl-elasticsearch/service/elasticsearch-api/update",
            dxl_client.last_request.destination_topic)
        self.assertEqual(
            {"index": "idx", "doc_type": "doc", "id": "42",
             "body": {"doc": {"name": "y"}}},
            dxl_client.last_payload)

    def test_not_found_error_reconstructed(self):
        dxl_client = FakeDxlClient(error_dict=_es_error(
            "NotFoundError",
            {"status_code": 404, "error": "not_found",
             "info": {"found": False, "_id": "42"}}))
        client = ElasticsearchClient(dxl_client)
        with self.assertRaises(NotFoundError) as context:
            client.get("idx", "doc", "42")
        self.assertEqual(404, context.exception.status_code)
        self.assertEqual("not_found", context.exception.error)
        self.assertEqual({"found": False, "_id": "42"},
                         context.exception.info)

    def test_nested_exception_info(self):
        dxl_client = FakeDxlClient(error_dict=_es_error(
            "ConnectionError",
            {"status_code": "N/A", "error": "connection refused",
             "info": {"class": "NewConnectionError",
                      "error": "Failed to establish a new connection"}}))
        client = ElasticsearchClient(dxl_client)
        with self.assertRaises(EsConnectionError) as context:
            client.get("idx", "doc", "42")
        info = context.exception.info
        self.assertEqual("NewConnectionError", info.__class__.__name__)
        self.assertEqual("Failed to establish a new connection", str(info))

    def test_transport_error_without_info(self):
        # Regression: a missing "info" element raised AttributeError
        dxl_client = FakeDxlClient(error_dict=_es_error(
            "TransportError", {"status_code": 500, "error": "boom"}))
        client = ElasticsearchClient(dxl_client)
        with self.assertRaises(TransportError) as context:
            client.get("idx", "doc", "42")
        self.assertEqual(500, context.exception.status_code)
        self.assertIsNone(context.exception.info)

    def test_exception_without_data(self):
        dxl_client = FakeDxlClient(error_dict=_es_error(
            "ImproperlyConfigured"))
        client = ElasticsearchClient(dxl_client)
        with self.assertRaises(Exception) as context:
            client.get("idx", "doc", "42")
        self.assertEqual("ImproperlyConfigured",
                         context.exception.__class__.__name__)

    def test_unknown_error_falls_back_to_generic_exception(self):
        for error_dict in ({"module": "other", "class": "X"},
                           _es_error("NoSuchExceptionClass")):
            dxl_client = FakeDxlClient(error_dict=error_dict)
            client = ElasticsearchClient(dxl_client)
            with self.assertRaises(Exception) as context:
                client.get("idx", "doc", "42")
            self.assertEqual(Exception, context.exception.__class__)
            self.assertEqual("Error: service error (1)",
                             str(context.exception))
