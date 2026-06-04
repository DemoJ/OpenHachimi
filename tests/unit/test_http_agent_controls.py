from openhachimi_agent.interface.http import app


def test_new_route_is_registered():
    assert any(route.path == "/new" and "POST" in route.methods for route in app.routes)


def test_stop_route_is_registered():
    assert any(route.path == "/stop" and "POST" in route.methods for route in app.routes)
