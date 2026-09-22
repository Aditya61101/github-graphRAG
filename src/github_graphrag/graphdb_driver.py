from neo4j import GraphDatabase

driver = None

class CreateDriver:
    def __init__(self, uri: str, username: str, password: str):
        self.uri = uri
        self.username = username
        self.password = password

    def get_driver(self) -> GraphDatabase.driver:
        global driver
        if not driver:
            driver =  GraphDatabase.driver(
                self.uri,
                auth=(self.username, self.password),
            )
        return driver