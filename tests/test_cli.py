from bitlaunch_mcp.cli import build_parser


def test_default_transport_is_stdio():
    args = build_parser().parse_args([])
    assert args.transport == "stdio"


def test_http_transport_args():
    args = build_parser().parse_args(
        ["--transport", "http", "--host", "0.0.0.0", "--port", "9000"]
    )
    assert args.transport == "http"
    assert args.host == "0.0.0.0"
    assert args.port == 9000
