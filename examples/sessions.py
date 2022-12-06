"""
How to use sessions to persist data.

Run this from terminal:
~$ python3 -m examples.sessions.py

And visit:
http://localhost:8080/

Enter values in form, terminate the script and start it again; values are
persisted as they are stored in session. See bottom of the script for three
different session storage variants.

"""
import json
from cheroot.wsgi import Server as wsgi_server

from wsgi import FileSession, LockingFileSession, MemorySession
from wsgi import WsgiApp, WsgiAppDispatcher, WsgiHttpResponse


class SessionPage(WsgiApp):
    def GET(self, path="/"):
        return f"""
        <html>
            <h1>Submit this</h1>
            <p>You visited {path}</p>
            <p>The form will be handled by OnePage.POST</p>
            <form method="post">
                <p>Set value for <code>self.session['firstname']</code>
                    <input type="text" name="firstname" placeholder="Put your name here">
                </p>
                <p>Append to <code>self.session['item_list']</code>
                    <input type="text" name="item_value">
                </p>
                <button type="submit">go</button>
            </form>
            <p>Current contents of session, dumped as JSON:</p>
            <pre>{json.dumps(dict(self.session.items()), indent=4)}</pre>
        </html>
        """

    def POST(self, path="/"):
        if self.form_data["firstname"]:
            # session auto-saves
            self.session["firstname"] = self.form_data["firstname"]

        if self.form_data["item_value"]:
            if not isinstance(self.session.get("item_list"), list):
                # auto-saves here
                self.session["item_list"] = []

            # accessing a mutable item, no auto-save
            self.session["item_list"].append(self.form_data["item_value"])
            self.session.save()

        # just refresh page
        headers = [("Location", path)]
        return WsgiHttpResponse(code=303, headers=headers)


# Stores a pickled dictionary in /tmp, not threadsafe
# SessionPage.session_class = FileSession("/tmp")

# Same as FileSession, but ensures that only one thread at a time can access
# a specific session ID
SessionPage.session_class = LockingFileSession("/tmp")

# This is what WsgiApp defaults to - nothing persists over restarts
# SessionPage.session_class = MemorySession

# catch absolutely every path and pass them on to SessionPage app
app = WsgiAppDispatcher([
    ("(.+)", SessionPage)
])
server = wsgi_server(("localhost", 8080), app)
server.start()
