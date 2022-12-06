"""
How to use WsgiApp to serve a single page.

Run this from terminal:
~$ python3 -m examples.single_page.py

And visit:
http://localhost:8080/
http://localhost:8080/any/other/path

"""
from cheroot.wsgi import Server as wsgi_server

from wsgi import WsgiApp, WsgiAppDispatcher


class OnePage(WsgiApp):
    def GET(self, path="/"):
        return f"""
        <html>
            <h1>Submit this</h1>
            <p>You visited {path}</p>
            <p>The form will be handled by OnePage.POST</p>
            <form method="post">
                <input required type="text" name="firstname" placeholder="Put your name here">
                <button type="submit">go</button>
            </form>
        </html>
        """

    def POST(self, path="/"):
        return f"""
        <html>
            <h1>Thank you {self.form_data.get('firstname')}</h1>
            <p>You visited {path}</p>
        </html>
        """


# catch absolutely every path and pass them on to OnePage app
app = WsgiAppDispatcher([
    ("(.+)", OnePage)
])
server = wsgi_server(("localhost", 8080), app)
server.start()
