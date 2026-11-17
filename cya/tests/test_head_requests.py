"""HEAD is answered as GET without a body (cya.core.middleware; issue #8 uptime monitors).

The test client already strips HEAD bodies, so body and header behaviour is checked against the
middleware directly with RequestFactory; real pages are checked through the client for status.
"""

from django.http import HttpResponse, StreamingHttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.views.decorators.http import require_http_methods

from cya.core.middleware import HeadAsGetMiddleware


@require_http_methods(["GET"])
def get_view(request):
    get_view.seen = request.method
    return HttpResponse(b"x" * 1234, headers={"X-Probe": "1"})


@require_http_methods(["GET", "POST"])
def form_view(request):
    if request.method == "GET":
        return HttpResponse(b"form")
    return HttpResponse(b"posted", status=201)  # never reached for HEAD


@require_http_methods(["POST"])
def post_only_view(request):
    return HttpResponse(b"posted")


def streaming_view(request):
    return StreamingHttpResponse(iter([b"chunk"] * 3))


class HeadAsGetMiddlewareTests(SimpleTestCase):
    def head(self, view):
        request = RequestFactory().head("/probe/")
        response = HeadAsGetMiddleware(view)(request)
        return request, response

    def test_a_get_view_answers_head_with_get_headers_and_no_body(self):
        request, response = self.head(get_view)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")
        self.assertEqual(response["Content-Length"], "1234")
        self.assertEqual(response["X-Probe"], "1")
        self.assertEqual(get_view.seen, "GET")
        self.assertEqual((request.method, request.META["REQUEST_METHOD"]), ("HEAD", "HEAD"))

    def test_a_get_and_post_view_runs_its_get_branch_never_its_post_branch(self):
        _, response = self.head(form_view)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Length"], "4")

    def test_a_view_that_does_not_allow_get_still_refuses(self):
        _, response = self.head(post_only_view)
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response["Allow"], "POST")
        self.assertEqual(response.content, b"")

    def test_a_streamed_body_is_dropped_unread(self):
        _, response = self.head(streaming_view)
        self.assertEqual(b"".join(response.streaming_content), b"")

    def test_other_methods_pass_through_unchanged(self):
        request = RequestFactory().post("/probe/")
        response = HeadAsGetMiddleware(form_view)(request)
        self.assertEqual((response.status_code, response.content), (201, b"posted"))


class HeadOnRealPagesTests(TestCase):
    def test_the_home_page_answers_head_like_get(self):
        # The uptime monitor's request: HEAD / while signed out (was 405).
        head, get = self.client.head("/"), self.client.get("/")
        self.assertEqual(head.status_code, get.status_code)
        self.assertNotEqual(head.status_code, 405)
        self.assertEqual(head.get("Location"), get.get("Location"))

    def test_head_needs_no_csrf_token_and_reaches_get_pages(self):
        client = self.client_class(enforce_csrf_checks=True)
        for path in ("/", "/auth/login/"):
            with self.subTest(path=path):
                self.assertNotEqual(client.head(path).status_code, 405)
