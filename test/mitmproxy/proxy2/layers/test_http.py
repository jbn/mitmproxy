import pytest

from mitmproxy.http import HTTPFlow, HTTPResponse
from mitmproxy.proxy.protocol.http import HTTPMode
from mitmproxy.proxy2 import layer
from mitmproxy.proxy2.commands import CloseConnection, OpenConnection, SendData
from mitmproxy.proxy2.events import ConnectionClosed, DataReceived
from mitmproxy.proxy2.layers import http, tls
from test.mitmproxy.proxy2.tutils import Placeholder, Playbook, reply, reply_next_layer


def test_http_proxy(tctx):
    """Test a simple HTTP GET / request"""
    server = Placeholder()
    flow = Placeholder()
    assert (
            Playbook(http.HttpLayer(tctx, HTTPMode.regular))
            >> DataReceived(tctx.client, b"GET http://example.com/foo?hello=1 HTTP/1.1\r\nHost: example.com\r\n\r\n")
            << http.HttpRequestHeadersHook(flow)
            >> reply()
            << http.HttpRequestHook(flow)
            >> reply()
            << OpenConnection(server)
            >> reply(None)
            << SendData(server, b"GET /foo?hello=1 HTTP/1.1\r\nHost: example.com\r\n\r\n")
            >> DataReceived(server, b"HTTP/1.1 200 OK\r\nContent-Length: 12\r\n\r\nHello World")
            << http.HttpResponseHeadersHook(flow)
            >> reply()
            >> DataReceived(server, b"!")
            << http.HttpResponseHook(flow)
            >> reply()
            << SendData(tctx.client, b"HTTP/1.1 200 OK\r\nContent-Length: 12\r\n\r\nHello World!")
    )
    assert server().address == ("example.com", 80)


@pytest.mark.parametrize("strategy", ["lazy", "eager"])
def test_https_proxy(strategy, tctx):
    """Test a CONNECT request, followed by a HTTP GET /"""
    server = Placeholder()
    flow = Placeholder()
    playbook = Playbook(http.HttpLayer(tctx, HTTPMode.regular))
    tctx.options.connection_strategy = strategy

    (playbook
     >> DataReceived(tctx.client, b"CONNECT example.proxy:80 HTTP/1.1\r\n\r\n")
     << http.HttpConnectHook(Placeholder())
     >> reply())
    if strategy == "eager":
        (playbook
         << OpenConnection(server)
         >> reply(None))
    (playbook
     << SendData(tctx.client, b'HTTP/1.1 200 Connection established\r\n\r\n')
     >> DataReceived(tctx.client, b"GET /foo?hello=1 HTTP/1.1\r\nHost: example.com\r\n\r\n")
     << layer.NextLayerHook(Placeholder())
     >> reply_next_layer(lambda ctx: http.HttpLayer(ctx, HTTPMode.transparent))
     << http.HttpRequestHeadersHook(flow)
     >> reply()
     << http.HttpRequestHook(flow)
     >> reply())
    if strategy == "lazy":
        (playbook
         << OpenConnection(server)
         >> reply(None))
    (playbook
     << SendData(server, b"GET /foo?hello=1 HTTP/1.1\r\nHost: example.com\r\n\r\n")
     >> DataReceived(server, b"HTTP/1.1 200 OK\r\nContent-Length: 12\r\n\r\nHello World!")
     << http.HttpResponseHeadersHook(flow)
     >> reply()
     << http.HttpResponseHook(flow)
     >> reply()
     << SendData(tctx.client, b"HTTP/1.1 200 OK\r\nContent-Length: 12\r\n\r\nHello World!"))
    assert playbook


@pytest.mark.parametrize("https_client", [False, True])
@pytest.mark.parametrize("https_server", [False, True])
@pytest.mark.parametrize("strategy", ["lazy", "eager"])
def test_redirect(strategy, https_server, https_client, tctx, monkeypatch):
    """Test redirects between http:// and https:// in regular proxy mode."""
    server = Placeholder()
    flow = Placeholder()
    tctx.options.connection_strategy = strategy
    p = Playbook(http.HttpLayer(tctx, HTTPMode.regular), hooks=False)

    if https_server:
        monkeypatch.setattr(tls, "ServerTLSLayer", tls.MockTLSLayer)

    def redirect(flow: HTTPFlow):
        if https_server:
            flow.request.url = "https://redirected.site/"
        else:
            flow.request.url = "http://redirected.site/"

    if https_client:
        p >> DataReceived(tctx.client, b"CONNECT example.com:80 HTTP/1.1\r\n\r\n")
        if strategy == "eager":
            p << OpenConnection(Placeholder())
            p >> reply(None)
        p << SendData(tctx.client, b'HTTP/1.1 200 Connection established\r\n\r\n')
        p >> DataReceived(tctx.client, b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n")
        p << layer.NextLayerHook(Placeholder())
        p >> reply_next_layer(lambda ctx: http.HttpLayer(ctx, HTTPMode.transparent))
    else:
        p >> DataReceived(tctx.client, b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n")
    p << http.HttpRequestHook(flow)
    p >> reply(side_effect=redirect)
    p << OpenConnection(server)
    p >> reply(None)
    p << SendData(server, b"GET / HTTP/1.1\r\nHost: redirected.site\r\n\r\n")
    p >> DataReceived(server, b"HTTP/1.1 200 OK\r\nContent-Length: 12\r\n\r\nHello World!")
    p << SendData(tctx.client, b"HTTP/1.1 200 OK\r\nContent-Length: 12\r\n\r\nHello World!")

    assert p
    if https_server:
        assert server().address == ("redirected.site", 443)
    else:
        assert server().address == ("redirected.site", 80)


def test_multiple_server_connections(tctx):
    """Test multiple requests being rewritten to different targets."""
    server1 = Placeholder()
    server2 = Placeholder()
    playbook = Playbook(http.HttpLayer(tctx, HTTPMode.regular), hooks=False)

    def redirect(to: str):
        def side_effect(flow: HTTPFlow):
            flow.request.url = to

        return side_effect

    assert (
            playbook
            >> DataReceived(tctx.client, b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n")
            << http.HttpRequestHook(Placeholder())
            >> reply(side_effect=redirect("http://one.redirect/"))
            << OpenConnection(server1)
            >> reply(None)
            << SendData(server1, b"GET / HTTP/1.1\r\nHost: one.redirect\r\n\r\n")
            >> DataReceived(server1, b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
            << SendData(tctx.client, b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
    )
    assert (
            playbook
            >> DataReceived(tctx.client, b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n")
            << http.HttpRequestHook(Placeholder())
            >> reply(side_effect=redirect("http://two.redirect/"))
            << OpenConnection(server2)
            >> reply(None)
            << SendData(server2, b"GET / HTTP/1.1\r\nHost: two.redirect\r\n\r\n")
            >> DataReceived(server2, b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
            << SendData(tctx.client, b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
    )
    assert server1().address == ("one.redirect", 80)
    assert server2().address == ("two.redirect", 80)


def test_http_reply_from_proxy(tctx):
    """Test a response served by mitmproxy itself."""

    def reply_from_proxy(flow: HTTPFlow):
        flow.response = HTTPResponse.make(418)

    assert (
            Playbook(http.HttpLayer(tctx, HTTPMode.regular), hooks=False)
            >> DataReceived(tctx.client, b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n")
            << http.HttpRequestHook(Placeholder())
            >> reply(side_effect=reply_from_proxy)
            << SendData(tctx.client, b"HTTP/1.1 418 I'm a teapot\r\ncontent-length: 0\r\n\r\n")
    )


def test_response_until_eof(tctx):
    """Test scenario where the server response body is terminated by EOF."""
    server = Placeholder()
    assert (
            Playbook(http.HttpLayer(tctx, HTTPMode.regular), hooks=False)
            >> DataReceived(tctx.client, b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n")
            << OpenConnection(server)
            >> reply(None)
            << SendData(server, b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n")
            >> DataReceived(server, b"HTTP/1.1 200 OK\r\n\r\nfoo")
            >> ConnectionClosed(server)
            << SendData(tctx.client, b"HTTP/1.1 200 OK\r\n\r\nfoo")
            << CloseConnection(tctx.client)
    )


def test_disconnect_while_intercept(tctx):
    """Test a server disconnect while a request is intercepted."""
    tctx.options.connection_strategy = "eager"

    server1 = Placeholder()
    server2 = Placeholder()
    flow = Placeholder()

    assert (
            Playbook(http.HttpLayer(tctx, HTTPMode.regular), hooks=False)
            >> DataReceived(tctx.client, b"CONNECT example.com:80 HTTP/1.1\r\n\r\n")
            << http.HttpConnectHook(Placeholder())
            >> reply()
            << OpenConnection(server1)
            >> reply(None)
            << SendData(tctx.client, b'HTTP/1.1 200 Connection established\r\n\r\n')
            >> DataReceived(tctx.client, b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n")
            << layer.NextLayerHook(Placeholder())
            >> reply_next_layer(lambda ctx: http.HttpLayer(ctx, HTTPMode.transparent))
            << http.HttpRequestHook(flow)
            >> ConnectionClosed(server1)
            >> reply(to=-2)
            << OpenConnection(server2)
            >> reply(None)
            << SendData(server2, b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n")
            >> DataReceived(server2, b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
            << SendData(tctx.client, b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
    )
    assert server1() != server2()
    assert flow().server_conn == server2()


def test_response_streaming(tctx):
    """Test HTTP response streaming"""
    server = Placeholder()
    flow = Placeholder()

    def enable_streaming(flow: HTTPFlow):
        flow.response.stream = lambda x: x.upper()

    assert (
            Playbook(http.HttpLayer(tctx, HTTPMode.regular), hooks=False)
            >> DataReceived(tctx.client, b"GET http://example.com/largefile HTTP/1.1\r\nHost: example.com\r\n\r\n")
            << OpenConnection(server)
            >> reply(None)
            << SendData(server, b"GET /largefile HTTP/1.1\r\nHost: example.com\r\n\r\n")
            >> DataReceived(server, b"HTTP/1.1 200 OK\r\nContent-Length: 6\r\n\r\nabc")
            << http.HttpResponseHeadersHook(flow)
            >> reply(side_effect=enable_streaming)
            << SendData(tctx.client, b"HTTP/1.1 200 OK\r\nContent-Length: 6\r\n\r\nABC")
            >> DataReceived(server, b"def")
            << SendData(tctx.client, b"DEF")
    )


@pytest.mark.parametrize("response", ["normal response", "early response", "early close", "early kill"])
def test_request_streaming(tctx, response):
    """
    Test HTTP request streaming

    This is a bit more contrived as we may receive server data while we are still sending the request.
    """
    server = Placeholder()
    flow = Placeholder()
    playbook = Playbook(http.HttpLayer(tctx, HTTPMode.regular), hooks=False)

    def enable_streaming(flow: HTTPFlow):
        flow.request.stream = lambda x: x.upper()

    assert (
            playbook
            >> DataReceived(tctx.client, b"POST http://example.com/ HTTP/1.1\r\n"
                                         b"Host: example.com\r\n"
                                         b"Content-Length: 6\r\n\r\n"
                                         b"abc")
            << http.HttpRequestHeadersHook(flow)
            >> reply(side_effect=enable_streaming)
            << OpenConnection(server)
            >> reply(None)
            << SendData(server, b"POST / HTTP/1.1\r\n"
                                b"Host: example.com\r\n"
                                b"Content-Length: 6\r\n\r\n"
                                b"ABC")
    )
    if response == "normal response":
        assert (
                playbook
                >> DataReceived(tctx.client, b"def")
                << SendData(server, b"DEF")
                >> DataReceived(server, b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
                << SendData(tctx.client, b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
        )
    elif response == "early response":
        # We may receive a response before we have finished sending our request.
        # We continue sending unless the server closes the connection.
        # https://tools.ietf.org/html/rfc7231#section-6.5.11
        assert (
                playbook
                >> DataReceived(server, b"HTTP/1.1 413 Request Entity Too Large\r\nContent-Length: 0\r\n\r\n")
                << SendData(tctx.client, b"HTTP/1.1 413 Request Entity Too Large\r\nContent-Length: 0\r\n\r\n")
                >> DataReceived(tctx.client, b"def")
                << SendData(server, b"DEF")
        )
    elif response == "early close":
        assert (
                playbook
                >> DataReceived(server, b"HTTP/1.1 413 Request Entity Too Large\r\nContent-Length: 0\r\n\r\n")
                << SendData(tctx.client, b"HTTP/1.1 413 Request Entity Too Large\r\nContent-Length: 0\r\n\r\n")
                >> ConnectionClosed(server)
                << CloseConnection(server)
                << CloseConnection(tctx.client)
        )
    elif response == "early kill":
        err = Placeholder()
        assert (
                playbook
                >> ConnectionClosed(server)
                << CloseConnection(server)
                << SendData(tctx.client, err)
                << CloseConnection(tctx.client)
        )
        assert b"502 Bad Gateway" in err()
    else:  # pragma: no cover
        assert False


@pytest.mark.parametrize("data", [
    None,
    b"I don't speak HTTP.",
    b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\nweee"
])
def test_server_aborts(tctx, data):
    """Test the scenario where the server doesn't serve a response"""
    server = Placeholder()
    flow = Placeholder()
    err = Placeholder()
    playbook = Playbook(http.HttpLayer(tctx, HTTPMode.regular), hooks=False)
    assert (
            playbook
            >> DataReceived(tctx.client, b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n")
            << OpenConnection(server)
            >> reply(None)
            << SendData(server, b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n")
    )
    if data:
        playbook >> DataReceived(server, data)
    assert (
            playbook
            >> ConnectionClosed(server)
            << CloseConnection(server)
            << http.HttpErrorHook(flow)
            >> reply()
            << SendData(tctx.client, err)
            << CloseConnection(tctx.client)
    )
    assert flow().error
    assert b"502 Bad Gateway" in err()