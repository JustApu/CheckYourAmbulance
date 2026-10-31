"""THROWAWAY spike: URL routes used by the web-layer tests."""
from django.http import HttpResponse, StreamingHttpResponse
from django.middleware.csrf import get_token
from django.urls import path


def login(request):
    request.session["user"] = "synthetic-sub-1"
    return HttpResponse("ok")


def post_check(request):
    if not request.session.get("user"):
        return HttpResponse("unauthenticated", status=401)
    return HttpResponse("check:1")


def sse_once(request):
    def events():
        yield ": connected\n\n"
        yield "event: check\nid: 1\ndata: check:42\n\n"
    return StreamingHttpResponse(events(), content_type="text/event-stream")


def csrf(request):
    return HttpResponse(get_token(request))


urlpatterns = [path("csrf", csrf), path("login", login), path("checks", post_check), path("sse-once", sse_once)]
