def get_db():
    """Get MongoDB database instance."""
    from app.extensions import mongo
    return mongo.db


def get_mongo_client():
    """Get MongoDB client."""
    from app.extensions import mongo
    return mongo.cx
