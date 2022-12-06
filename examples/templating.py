"""
How to use WsgiApp.render_template to return jinja2 templates.

Run this from terminal:
~$ python3 -m examples.templating.py

And visit:
http://localhost:8080/
http://localhost:8080/any/other/path

"""
from cheroot.wsgi import Server as wsgi_server

from wsgi import WsgiApp, WsgiAppDispatcher, WsgiBadRequest, json_response


class OneTemplatedPage(WsgiApp):
    template_path = "examples/templates/"

    def GET(self, path="/"):
        return self.render_template("index.html")

    def POST(self, path="/"):
        return self.render_template("post_result.html")


# catch absolutely every path and pass them on to OnePage app
app = WsgiAppDispatcher([
    ("(.+)", OneTemplatedPage)
])
server = wsgi_server(("localhost", 8080), app)
server.start()
