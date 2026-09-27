class BaseRepository:
    """Shared persistence helpers."""

    def __init__(self, session):
        self.session = session

    def save(self, entity):
        self.session.add(entity)
        self.session.commit()
        return entity
