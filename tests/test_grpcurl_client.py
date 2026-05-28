import socket
import sys
import unittest
from concurrent import futures
from pathlib import Path

import grpc

from sms_tool.grpcurl_client import call_grpcurl


ROOT = Path(__file__).resolve().parents[1]
GOPAY_FLOW = ROOT / "services" / "gopay-flow"
sys.path.insert(0, str(GOPAY_FLOW))

import payment_pb2  # noqa: E402
import payment_pb2_grpc  # noqa: E402


class _PaymentServicer(payment_pb2_grpc.PaymentServiceServicer):
    def __init__(self):
        self.requests = []

    def StartGoPay(self, request, context):
        self.requests.append(request)
        return payment_pb2.StartGoPayResponse(
            success=True,
            flow_id="flow_python_grpc",
            gopay_phone="+6281234567890",
        )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class GrpcUrlClientTests(unittest.TestCase):
    def test_call_grpcurl_falls_back_to_python_payment_client(self):
        servicer = _PaymentServicer()
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=1))
        payment_pb2_grpc.add_PaymentServiceServicer_to_server(servicer, server)
        port = _free_port()
        server.add_insecure_port(f"127.0.0.1:{port}")
        server.start()
        try:
            result = call_grpcurl(
                "StartGoPay",
                {
                    "credential": {"session_token": "st_test", "access_token": "at_test"},
                    "gopay_phone": "81234567890",
                },
                addr=f"127.0.0.1:{port}",
                service="payment.PaymentService",
                grpcurl="definitely-missing-grpcurl",
                proto_path="services\\gopay-flow\\proto\\payment.proto",
                proto_import_path="services\\gopay-flow\\proto",
                timeout_seconds=5,
            )
        finally:
            server.stop(0)

        self.assertTrue(result["success"])
        self.assertEqual(result["flowId"], "flow_python_grpc")
        self.assertEqual(result["gopayPhone"], "+6281234567890")
        self.assertEqual(servicer.requests[0].credential.session_token, "st_test")
        self.assertEqual(servicer.requests[0].gopay_phone, "81234567890")


if __name__ == "__main__":
    unittest.main()
