"""Answer HEAD exactly as GET, without a body (RFC 9110 9.3.2; issue #8).

Found in the issue #8 DEP-13 rehearsal: hosted uptime monitors commonly check a site with
`HEAD /`, and every page view here allows GET (or GET and POST) but not HEAD, so a monitor saw
405 on a healthy site. Adding HEAD to each view's allowed methods would be wrong for the views
that treat any non-GET request as a form post. Instead this middleware, placed first, runs a HEAD
request as GET through every later layer and the view, then removes the body from the response.
The status and headers, Content-Length included, are those GET would send.

A GET view must be safe (it changes nothing), so running it for HEAD changes nothing either.
A view that does not allow GET still answers 405, with its own Allow header.
"""


class HeadAsGetMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method != "HEAD":
            return self.get_response(request)
        request.method = "GET"
        request.META["REQUEST_METHOD"] = "GET"
        try:
            response = self.get_response(request)
        finally:
            request.method = "HEAD"
            request.META["REQUEST_METHOD"] = "HEAD"
        if response.streaming:
            # Never iterate a streamed body just to measure it: drop it unread. (Not
            # response.close(): that sends request_finished while the request is still running.)
            response.streaming_content = []
            return response
        length = response.get("Content-Length") or str(len(response.content))
        response.content = b""
        response["Content-Length"] = length
        return response
