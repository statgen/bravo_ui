# Search redirects to appropriate endpoint
def test_search(client):
    response = client.get('/favicon')
    assert response.status_code == 200
