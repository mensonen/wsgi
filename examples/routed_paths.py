"""
How to use WsgiApp.route to map paths into individual methods.

Run this from terminal:
~$ python3 -m examples.routed_paths.py

And visit:
http://localhost:8080/customer_service/customer/23/address/40938
http://localhost:8080/cart_service/cart

"""
import json

# any python-based WSGI server will do
from cheroot.wsgi import Server as wsgi_server
from functools import wraps

from wsgi import WsgiApp, WsgiAppDispatcher, WsgiBadRequest, json_response


def catch_errors(f):
    """A sample "catch-all-errors" wrapper that always returns JSON."""
    @wraps(f)
    def wrapper(self, *args, **kwargs):
        try:
            return f(self, *args, **kwargs)
        except Exception as e:
            headers = [("Content-Type", "application/json")]
            data = json.dumps({"error": repr(e)})

            return WsgiBadRequest(data=data, headers=headers)

    return wrapper


class CustomerApp(WsgiApp):
    """A sample app that just echoes input back as JSON."""

    @WsgiApp.route("customer/<int:customer_id>")
    @catch_errors
    def get_customer(self, customer_id=None):
        return json_response(path=self.url.path, customer=customer_id)

    @WsgiApp.route("customer", methods=["POST"])
    @catch_errors
    def create_customer(self):
        return json_response(path=self.url.path, customer_data=self.json)

    @WsgiApp.route("customer/<int:customer_id>/address/<int:address_id>")
    @catch_errors
    def get_customer_address(self, customer_id=None, address_id=None):
        return json_response(path=self.url.path,
                             customer=customer_id,
                             address=address_id)

    @WsgiApp.route("customer/<int:customer_id>", methods="DELETE")
    @catch_errors
    def delete_customer(self, customer_id):
        # the catch_errors wrapper ensures that JSON is stil lreturned
        raise ValueError(f"Invalid customer ID {customer_id}")


class CartApp(WsgiApp):
    @WsgiApp.route("cart")
    def get_cart(self):
        return json_response(path=self.url.path, cart=[])


app = WsgiAppDispatcher([
    ("/customer_service/(.*)", CustomerApp),
    ("/cart_service/(.*)", CartApp),
])
server = wsgi_server(("localhost", 8080), app)
server.start()
