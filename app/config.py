import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    """Base configuration"""
    SECRET_KEY = os.environ.get("SECRET_KEY") or "your-secret-key-change-in-production"
    
    # MongoDB Configuration
    MONGO_URI = os.environ.get("MONGO_URI") or "mongodb+srv://brifixinvestor:Donsaale5722@brifix-investor.g7snl.mongodb.net/?appName=brifix-investor"
    MONGO_DBNAME = os.environ.get("MONGO_DBNAME") or "brifix_investors"
    MONGO_USERNAME = os.environ.get("MONGO_USERNAME") or None
    MONGO_PASSWORD = os.environ.get("MONGO_PASSWORD") or None

class DevelopmentConfig(Config):
    """Development configuration"""
    DEBUG = True
    TESTING = False
    MONGO_URI = os.environ.get("MONGO_URI") or "mongodb+srv://brifixinvestor:Donsaale5722@brifix-investor.g7snl.mongodb.net/?appName=brifix-investor"

class TestingConfig(Config):
    """Testing configuration"""
    TESTING = True
    MONGO_URI = "mongodb+srv://brifixinvestor:Donsaale5722@brifix-investor.g7snl.mongodb.net/?appName=brifix-investor"

class ProductionConfig(Config):
    """Production configuration"""
    DEBUG = False
    TESTING = False
