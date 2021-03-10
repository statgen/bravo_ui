# Search redirects to appropriate endpoint
def test_favicon_endpoint(client):
    response = client.get('/favicon')
    assert response.status_code == 200


def test_favicon_file(client):
    response = client.get('/favicon.ico')
    assert response.status_code == 200
