from bson import ObjectId
from datetime import datetime

def serialize_mongo(data):
    if isinstance(data, list):
        return [serialize_mongo(item) for item in data]
    elif isinstance(data, dict):
        return {key: serialize_mongo(value) for key, value in data.items()}
    elif isinstance(data, ObjectId):
        return str(data)
    elif isinstance(data, bytes):
        try:
            return data.decode('utf-8')
        except UnicodeDecodeError:
            return data.hex()
    elif isinstance(data, datetime):
        return data.isoformat()
    else:
        return data