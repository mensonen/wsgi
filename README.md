# A lightweight WSGI app framework

This module provides (fairly) simple WSGI app implementation that automates a
lot of tedious tasks in an WSGI app creation, such as reading the env,
dispatching requests to proper handler based on URLs, catching errors etc.

## Basic usage

In order to create a web app/service that can respond to actual HTTP requests,
three items are required:

 1. (At least) one instance of `WsgiApp`, which contains the actual application
    logic
 2. One instance of `WsgiAppDispatcher`, which maps incoming requests based on
    paths to WSGI apps
 3. A WSGI-compliant web server; either a python implementation that can be run
    directly from a python script, e.g cheroot or flup, or an external web
    server, e.g Apache or Nginx

A `WsgiApp` can be utilised in two different ways; either one class instance
per path (similar style as when building basic services using Python's inbuilt
http.server), or with one class instance for the entire app, with one method
for each path (similar to flask and others). Both ways can be utilised at the
same time and multiple instances of both can co-exist simultaneously, which is
what `WsgiAppDispatcher` is used for.

To build a basic instance-per-path app, WsgiApp needs to be inherited and one
method for GET and/or POST needs to be defined:

```python
from cheroot.wsgi import Server
import wsgi

class OnePage(wsgi.WsgiApp):
    def GET(self, path):
        return str("<h1>Hello</h1>"), [("Content-Type", "text/html")]

    def POST(self, path):
        return str("<h1>Thank You</h1>"), [("Content-Type", "text/html")]

# map all URLs to OnePage, catching the part after /onepage/
urls = [("/onepage/(.*)?", OnePage)]

app = wsgi.WsgiAppDispatcher(urls)
server = Server(("localhost", 8080), app)
server.start()
```

To create an app that has a method for each request path, use `WsgiApp.route`:


```python
from wsgi import WsgiApp, WsgiAppDispatcher, json_response

class MultiPage(WsgiApp):

    # this matches /multipath/customers
    @WsgiApp.route("customers")
    def get_customers(self):
        return json_response()

    @WsgiApp.route("customers", methods=["PUT"])
    def create_customer(self):
        return json_response()

    @WsgiApp.route("customer/<int:customer_id>")
    def get_customer(self, customer_id):
        return json_response(customer_data=customer_id)

app = WsgiAppDispatcher([
    ("/multipath/(.+)", MultiPage)
])
```


## Output helper methods

Templating is available if the `jinja2` python module is installed. From any
method inside WsgiApp, `self.render_template` can be called and its return
value can be passed directly to browser.

WSGI apps often need to return JSON to browser. For this the `wsgi.json_response`
shorthand exists, which acts quite similarly to Flask's json response method.

In cases where an app needs to return a status that's not "200 OK", either an
instance of `WsgiHttpError` (or one if its subclasses) can be raised, or an
instance of `WsgiHttpResponse` (or one if its subclasses) can be returned. These
classes set the response status and headers automatically.

## Input helper methods

Input can also be automatically parsed.

 * `self.args` contains request URL query parameters as a dictionary
 * `self.url` contains a named tuple, which is the parse result of
    urllib.parse.urlparse
 * `self.form_data` contains request form data as nested a dictionary of
    key-value pairs
 * `self.json` contains the request body as JSON parsed dictionary
