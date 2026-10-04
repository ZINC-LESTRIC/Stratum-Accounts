from datetime import datetime, date, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
import os, hashlib, hmac, secrets, json, csv, io, re, urllib.request

from fastapi import FastAPI, Depends, HTTPException, Header, UploadFile, File
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import create_engine, String, DateTime, Date, ForeignKey, Numeric, Boolean, Text, UniqueConstraint, select, func, and_
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session
import jwt

DB_URL = os.getenv('DATABASE_URL', 'sqlite:///./reconai.db')
if DB_URL.startswith('postgres://'):
    DB_URL = DB_URL.replace('postgres://', 'postgresql+psycopg://', 1)
elif DB_URL.startswith('postgresql://') and '+psycopg' not in DB_URL:
    DB_URL = DB_URL.replace('postgresql://', 'postgresql+psycopg://', 1)

AUTO_CREATE = os.getenv('RECONAI_AUTO_CREATE_SCHEMA', 'true').lower() == 'true'
CORS_ORIGINS = [o.strip() for o in os.getenv('CORS_ORIGINS', '*').split(',') if o.strip()]
engine = create_engine(DB_URL, connect_args={'check_same_thread': False} if DB_URL.startswith('sqlite') else {})
JWT_SECRET = os.getenv('RECONAI_JWT_SECRET', 'dev-only-change-this-secret-key-please-set-a-strong-production-secret-123456')
JWT_ALG = 'HS256'
TOKEN_HOURS = 24

class Base(DeclarativeBase): pass

class User(Base):
    __tablename__='users'
    id:Mapped[int]=mapped_column(primary_key=True)
    email:Mapped[str]=mapped_column(String(255),unique=True,index=True)
    password_hash:Mapped[str]=mapped_column(String(255))
    created_at:Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow)

app = FastAPI(title='ReconAI', version='4.0.0')

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS if CORS_ORIGINS != ['*'] else ['*'],
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)

@app.on_event("startup")
def on_startup():
    if AUTO_CREATE:
        try:
            Base.metadata.create_all(engine)
            print("ReconAI schema created/verified")
        except Exception as e:
            print(f"Schema create warning: {e}")

@app.get('/health')
def health():
    return {'status':'ok','service':'reconai','version':'4.0.0'}

@app.get('/')
def root():
    return FileResponse('static/index.html')

@app.get('/api')
def api_info():
    return {'message': 'ReconAI API is running', 'docs': '/docs'}
