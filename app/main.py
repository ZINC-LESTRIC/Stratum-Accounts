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
# Railway / many hosts inject postgres:// — normalize for SQLAlchemy + psycopg3
if DB_URL.startswith('postgres://'):
    DB_URL = DB_URL.replace('postgres://', 'postgresql+psycopg://', 1)
elif DB_URL.startswith('postgresql://') and '+psycopg' not in DB_URL:
    DB_URL = DB_URL.replace('postgresql://', 'postgresql+psycopg://', 1)

AUTO_CREATE = os.getenv('RECONAI_AUTO_CREATE_SCHEMA', 'true').lower() == 'true'
DEV_HEADER_AUTH = os.getenv('RECONAI_DEV_HEADER_AUTH', 'false').lower() == 'true'
CORS_ORIGINS = [o.strip() for o in os.getenv(
    'CORS_ORIGINS',
    'http://localhost:3000,http://127.0.0.1:3000,http://localhost:5173,http://127.0.0.1:8000'
).split(',') if o.strip()]
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

class Business(Base):
    __tablename__='businesses'
    id:Mapped[int]=mapped_column(primary_key=True)
    name:Mapped[str]=mapped_column(String(255))
    business_type:Mapped[str]=mapped_column(String(100),default='Food Stall')
    currency:Mapped[str]=mapped_column(String(10),default='PKR')
    owner_id:Mapped[int]=mapped_column(ForeignKey('users.id'))
    created_at:Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow)

class BusinessUser(Base):
    __tablename__='business_users'
    id:Mapped[int]=mapped_column(primary_key=True)
    business_id:Mapped[int]=mapped_column(ForeignKey('businesses.id'),index=True)
    user_id:Mapped[int]=mapped_column(ForeignKey('users.id'),index=True)
    role:Mapped[str]=mapped_column(String(30),default='owner')
    __table_args__=(UniqueConstraint('business_id','user_id'),)

class SessionToken(Base):
    __tablename__='session_tokens'
    id:Mapped[int]=mapped_column(primary_key=True)
    user_id:Mapped[int]=mapped_column(ForeignKey('users.id'),index=True)
    token_hash:Mapped[str]=mapped_column(String(128),unique=True,index=True)
    expires_at:Mapped[datetime]=mapped_column(DateTime,index=True)
    revoked_at:Mapped[Optional[datetime]]=mapped_column(DateTime,nullable=True)

class Account(Base):
    __tablename__='accounts'
    id:Mapped[int]=mapped_column(primary_key=True)
    business_id:Mapped[int]=mapped_column(ForeignKey('businesses.id'),index=True)
    code:Mapped[str]=mapped_column(String(30))
    name:Mapped[str]=mapped_column(String(120))
    type:Mapped[str]=mapped_column(String(30))
    normal_balance:Mapped[str]=mapped_column(String(6))
    active:Mapped[bool]=mapped_column(Boolean,default=True)
    __table_args__=(UniqueConstraint('business_id','code'),UniqueConstraint('business_id','name'))

class JournalEntry(Base):
    __tablename__='journal_entries'
    id:Mapped[int]=mapped_column(primary_key=True)
    business_id:Mapped[int]=mapped_column(ForeignKey('businesses.id'),index=True)
    txn_date:Mapped[date]=mapped_column(Date)
    source_type:Mapped[str]=mapped_column(String(50))
    source_id:Mapped[Optional[int]]=mapped_column(nullable=True)
    description:Mapped[str]=mapped_column(Text)
    status:Mapped[str]=mapped_column(String(20),default='posted')
    reversal_of_id:Mapped[Optional[int]]=mapped_column(ForeignKey('journal_entries.id'),nullable=True)
    created_by:Mapped[int]=mapped_column(ForeignKey('users.id'))
    created_at:Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow)

class JournalLine(Base):
    __tablename__='journal_entry_lines'
    id:Mapped[int]=mapped_column(primary_key=True)
    journal_entry_id:Mapped[int]=mapped_column(ForeignKey('journal_entries.id'),index=True)
    account_id:Mapped[int]=mapped_column(ForeignKey('accounts.id'))
    debit:Mapped[Decimal]=mapped_column(Numeric(18,2),default=0)
    credit:Mapped[Decimal]=mapped_column(Numeric(18,2),default=0)
    description:Mapped[str]=mapped_column(Text,default='')

class Product(Base):
    __tablename__='products'
    id:Mapped[int]=mapped_column(primary_key=True)
    business_id:Mapped[int]=mapped_column(ForeignKey('businesses.id'),index=True)
    name:Mapped[str]=mapped_column(String(150))
    sku:Mapped[str]=mapped_column(String(80),default='')
    unit:Mapped[str]=mapped_column(String(30),default='pcs')
    unit_cost:Mapped[Decimal]=mapped_column(Numeric(18,2),default=0)
    opening_quantity:Mapped[Decimal]=mapped_column(Numeric(18,3),default=0)
    active:Mapped[bool]=mapped_column(Boolean,default=True)
    __table_args__=(UniqueConstraint('business_id','sku'),)

class Customer(Base):
    __tablename__='customers'
    id:Mapped[int]=mapped_column(primary_key=True)
    business_id:Mapped[int]=mapped_column(ForeignKey('businesses.id'),index=True)
    name:Mapped[str]=mapped_column(String(180))
    phone:Mapped[str]=mapped_column(String(50),default='')
    email:Mapped[str]=mapped_column(String(255),default='')
    active:Mapped[bool]=mapped_column(Boolean,default=True)

class Supplier(Base):
    __tablename__='suppliers'
    id:Mapped[int]=mapped_column(primary_key=True)
    business_id:Mapped[int]=mapped_column(ForeignKey('businesses.id'),index=True)
    name:Mapped[str]=mapped_column(String(180))
    phone:Mapped[str]=mapped_column(String(50),default='')
    email:Mapped[str]=mapped_column(String(255),default='')
    active:Mapped[bool]=mapped_column(Boolean,default=True)

class Sale(Base):
    __tablename__='sales'
    id:Mapped[int]=mapped_column(primary_key=True)
    business_id:Mapped[int]=mapped_column(ForeignKey('businesses.id'),index=True)
    txn_date:Mapped[date]=mapped_column(Date)
    product_id:Mapped[int]=mapped_column(ForeignKey('products.id'))
    quantity:Mapped[Decimal]=mapped_column(Numeric(18,3))
    unit_price:Mapped[Decimal]=mapped_column(Numeric(18,2))
    discount:Mapped[Decimal]=mapped_column(Numeric(18,2),default=0)
    payment_method:Mapped[str]=mapped_column(String(30))
    customer:Mapped[Optional[str]]=mapped_column(String(180),nullable=True)
    notes:Mapped[Optional[str]]=mapped_column(Text,nullable=True)
    total:Mapped[Decimal]=mapped_column(Numeric(18,2))
    cogs:Mapped[Decimal]=mapped_column(Numeric(18,2),default=0)
    status:Mapped[str]=mapped_column(String(20),default='posted')
    idempotency_key:Mapped[Optional[str]]=mapped_column(String(100),nullable=True)
    journal_entry_id:Mapped[Optional[int]]=mapped_column(ForeignKey('journal_entries.id'),nullable=True)

class Purchase(Base):
    __tablename__='purchases'
    id:Mapped[int]=mapped_column(primary_key=True)
    business_id:Mapped[int]=mapped_column(ForeignKey('businesses.id'),index=True)
    txn_date:Mapped[date]=mapped_column(Date)
    supplier:Mapped[str]=mapped_column(String(180))
    product_id:Mapped[int]=mapped_column(ForeignKey('products.id'))
    quantity:Mapped[Decimal]=mapped_column(Numeric(18,3))
    unit_cost:Mapped[Decimal]=mapped_column(Numeric(18,2))
    total:Mapped[Decimal]=mapped_column(Numeric(18,2))
    payment_method:Mapped[str]=mapped_column(String(30))
    invoice_number:Mapped[str]=mapped_column(String(100),default='')
    notes:Mapped[Optional[str]]=mapped_column(Text,nullable=True)
    status:Mapped[str]=mapped_column(String(20),default='posted')
    idempotency_key:Mapped[Optional[str]]=mapped_column(String(100),nullable=True)
    journal_entry_id:Mapped[Optional[int]]=mapped_column(ForeignKey('journal_entries.id'),nullable=True)

class Expense(Base):
    __tablename__='expenses'
    id:Mapped[int]=mapped_column(primary_key=True)
    business_id:Mapped[int]=mapped_column(ForeignKey('businesses.id'),index=True)
    txn_date:Mapped[date]=mapped_column(Date)
    category:Mapped[str]=mapped_column(String(80))
    description:Mapped[str]=mapped_column(Text)
    amount:Mapped[Decimal]=mapped_column(Numeric(18,2))
    payment_method:Mapped[str]=mapped_column(String(30))
    vendor:Mapped[Optional[str]]=mapped_column(String(180),nullable=True)
    reference:Mapped[Optional[str]]=mapped_column(String(100),nullable=True)
    notes:Mapped[Optional[str]]=mapped_column(Text,nullable=True)
    status:Mapped[str]=mapped_column(String(20),default='posted')
    idempotency_key:Mapped[Optional[str]]=mapped_column(String(100),nullable=True)
    journal_entry_id:Mapped[Optional[int]]=mapped_column(ForeignKey('journal_entries.id'),nullable=True)

class InventoryMovement(Base):
    __tablename__='inventory_movements'
    id:Mapped[int]=mapped_column(primary_key=True)
    business_id:Mapped[int]=mapped_column(ForeignKey('businesses.id'),index=True)
    product_id:Mapped[int]=mapped_column(ForeignKey('products.id'))
    txn_date:Mapped[date]=mapped_column(Date)
    movement_type:Mapped[str]=mapped_column(String(30))
    quantity:Mapped[Decimal]=mapped_column(Numeric(18,3))
    unit_cost:Mapped[Decimal]=mapped_column(Numeric(18,2),default=0)
    source_type:Mapped[str]=mapped_column(String(50))
    source_id:Mapped[Optional[int]]=mapped_column(nullable=True)

class InventoryCount(Base):
    __tablename__='inventory_counts'
    id:Mapped[int]=mapped_column(primary_key=True)
    business_id:Mapped[int]=mapped_column(ForeignKey('businesses.id'),index=True)
    product_id:Mapped[int]=mapped_column(ForeignKey('products.id'))
    count_date:Mapped[date]=mapped_column(Date)
    actual_qty:Mapped[Decimal]=mapped_column(Numeric(18,3))
    notes:Mapped[Optional[str]]=mapped_column(Text,nullable=True)

class CashCount(Base):
    __tablename__='cash_counts'
    id:Mapped[int]=mapped_column(primary_key=True)
    business_id:Mapped[int]=mapped_column(ForeignKey('businesses.id'),index=True)
    count_date:Mapped[date]=mapped_column(Date)
    actual_cash:Mapped[Decimal]=mapped_column(Numeric(18,2))
    notes:Mapped[Optional[str]]=mapped_column(Text,nullable=True)

class BankAccount(Base):
    __tablename__='bank_accounts'
    id:Mapped[int]=mapped_column(primary_key=True)
    business_id:Mapped[int]=mapped_column(ForeignKey('businesses.id'),index=True)
    name:Mapped[str]=mapped_column(String(120))
    account_number_masked:Mapped[str]=mapped_column(String(40),default='')
    currency:Mapped[str]=mapped_column(String(10),default='PKR')
    active:Mapped[bool]=mapped_column(Boolean,default=True)

class BankTransaction(Base):
    __tablename__='bank_transactions'
    id:Mapped[int]=mapped_column(primary_key=True)
    business_id:Mapped[int]=mapped_column(ForeignKey('businesses.id'),index=True)
    bank_account_id:Mapped[int]=mapped_column(ForeignKey('bank_accounts.id'))
    txn_date:Mapped[date]=mapped_column(Date)
    description:Mapped[str]=mapped_column(Text)
    amount:Mapped[Decimal]=mapped_column(Numeric(18,2))
    direction:Mapped[str]=mapped_column(String(10))
    reference:Mapped[str]=mapped_column(String(120),default='')
    matched_journal_id:Mapped[Optional[int]]=mapped_column(ForeignKey('journal_entries.id'),nullable=True)
    status:Mapped[str]=mapped_column(String(30),default='unmatched')
    imported:Mapped[bool]=mapped_column(Boolean,default=False)

class ReconciliationSession(Base):
    __tablename__='reconciliation_sessions'
    id:Mapped[int]=mapped_column(primary_key=True)
    business_id:Mapped[int]=mapped_column(ForeignKey('businesses.id'),index=True)
    recon_type:Mapped[str]=mapped_column(String(30))
    asof:Mapped[date]=mapped_column(Date)
    status:Mapped[str]=mapped_column(String(30))
    created_at:Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow)

class ReconciliationItem(Base):
    __tablename__='reconciliation_items'
    id:Mapped[int]=mapped_column(primary_key=True)
    session_id:Mapped[int]=mapped_column(ForeignKey('reconciliation_sessions.id'))
    source_type:Mapped[str]=mapped_column(String(50))
    source_id:Mapped[int]=mapped_column()
    status:Mapped[str]=mapped_column(String(30))
    difference:Mapped[Decimal]=mapped_column(Numeric(18,2),default=0)
    explanation:Mapped[Optional[str]]=mapped_column(Text,nullable=True)

class AuditLog(Base):
    __tablename__='audit_logs'
    id:Mapped[int]=mapped_column(primary_key=True)
    business_id:Mapped[int]=mapped_column(ForeignKey('businesses.id'),index=True)
    user_id:Mapped[int]=mapped_column(ForeignKey('users.id'))
    action:Mapped[str]=mapped_column(String(80))
    record_type:Mapped[str]=mapped_column(String(80))
    record_id:Mapped[str]=mapped_column(String(80))
    original_value:Mapped[Optional[str]]=mapped_column(Text,nullable=True)
    new_value:Mapped[Optional[str]]=mapped_column(Text,nullable=True)
    reason:Mapped[Optional[str]]=mapped_column(Text,nullable=True)
    created_at:Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow)

class AIConversation(Base):
    __tablename__='ai_conversations'
    id:Mapped[int]=mapped_column(primary_key=True)
    business_id:Mapped[int]=mapped_column(ForeignKey('businesses.id'),index=True)
    user_id:Mapped[int]=mapped_column(ForeignKey('users.id'))
    created_at:Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow)

class AIMessage(Base):
    __tablename__='ai_messages'
    id:Mapped[int]=mapped_column(primary_key=True)
    conversation_id:Mapped[int]=mapped_column(ForeignKey('ai_conversations.id'))
    role:Mapped[str]=mapped_column(String(20))
    content:Mapped[str]=mapped_column(Text)
    created_at:Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow)

MONEY=Decimal('0.01')
QTY=Decimal('0.001')

# ---------- deterministic helpers ----------
def money(x): return Decimal(str(x)).quantize(MONEY,rounding=ROUND_HALF_UP)
def qty(x): return Decimal(str(x)).quantize(QTY,rounding=ROUND_HALF_UP)
def hash_pw(p):
    salt=secrets.token_bytes(16)
    return salt.hex()+':'+hashlib.pbkdf2_hmac('sha256',p.encode(),salt,180000).hex()
def verify_pw(p,h):
    try:
        s,d=h.split(':'); salt=bytes.fromhex(s)
        return hmac.compare_digest(hashlib.pbkdf2_hmac('sha256',p.encode(),salt,180000).hex(),d)
    except Exception: return False
def token_hash(token): return hashlib.sha256(token.encode()).hexdigest()
def issue_token(db,user_id):
    now=datetime.now(timezone.utc); exp=now+timedelta(hours=TOKEN_HOURS)
    st=SessionToken(user_id=user_id,token_hash='pending',expires_at=exp.replace(tzinfo=None)); db.add(st); db.flush()
    raw=jwt.encode({'sub':str(user_id),'sid':st.id,'iat':int(now.timestamp()),'exp':int(exp.timestamp())},JWT_SECRET,algorithm=JWT_ALG)
    st.token_hash=token_hash(raw)
    return raw, exp

def seed_accounts(db,bid):
    rows=[('1000','Cash','Asset','debit'),('1010','Bank','Asset','debit'),('1100','Accounts Receivable','Asset','debit'),('1200','Inventory','Asset','debit'),('1500','Equipment','Asset','debit'),('2000','Accounts Payable','Liability','credit'),('2100','Loans','Liability','credit'),('3000','Owner Capital','Equity','credit'),('3100','Owner Drawings','Equity','debit'),('3200','Retained Earnings','Equity','credit'),('4000','Food Sales','Revenue','credit'),('4100','Other Revenue','Revenue','credit'),('5000','Cost of Goods Sold','Expense','debit'),('5100','Rent','Expense','debit'),('5200','Utilities','Expense','debit'),('5300','Salaries','Expense','debit'),('5400','Packaging','Expense','debit'),('5500','Transport','Expense','debit'),('5600','Marketing','Expense','debit'),('5700','Repairs','Expense','debit'),('5900','Other Expenses','Expense','debit'),('5910','Inventory Adjustments','Expense','debit')]
    for code,name,t,n in rows: db.add(Account(business_id=bid,code=code,name=name,type=t,normal_balance=n))
    db.flush()
def acct(db,bid,name):
    a=db.scalar(select(Account).where(Account.business_id==bid,Account.name==name))
    if not a: raise HTTPException(500,f'Missing account: {name}')
    return a

def post_journal(db,bid,user_id,txn_date,source_type,source_id,description,lines):
    if not lines: raise HTTPException(400,'Journal requires lines')
    clean=[]; debit=Decimal(0); credit=Decimal(0)
    for aid,d,c,desc in lines:
        d=money(d); c=money(c)
        if d<0 or c<0 or (d>0 and c>0): raise HTTPException(400,'Invalid journal line')
        if db.scalar(select(Account).where(Account.id==aid,Account.business_id==bid)) is None: raise HTTPException(400,'Account does not belong to business')
        debit+=d; credit+=c; clean.append((aid,d,c,desc))
    if debit<=0 or debit!=credit: raise HTTPException(400,f'Unbalanced journal entry: debits={debit}, credits={credit}')
    je=JournalEntry(business_id=bid,txn_date=txn_date,source_type=source_type,source_id=source_id,description=description,created_by=user_id)
    db.add(je); db.flush()
    for aid,d,c,desc in clean: db.add(JournalLine(journal_entry_id=je.id,account_id=aid,debit=d,credit=c,description=desc))
    return je

def payment_account(db,bid,method,purpose='sale'):
    if purpose == 'sale':
        m={'Cash':'Cash','Bank':'Bank','Card':'Bank','Mobile wallet':'Bank','Credit':'Accounts Receivable'}
    else:
        m={'Cash':'Cash','Bank':'Bank','Card':'Bank','Mobile wallet':'Bank','Credit':'Accounts Payable'}
    if method not in m: raise HTTPException(400,'Unsupported payment method')
    return acct(db,bid,m[method])
def audit(db,bid,user_id,action,typ,rid,new=None,old=None,reason=None):
    db.add(AuditLog(business_id=bid,user_id=user_id,action=action,record_type=typ,record_id=str(rid),original_value=old,new_value=new,reason=reason))

def membership(db,bid,user):
    return db.scalar(select(BusinessUser).where(BusinessUser.business_id==bid,BusinessUser.user_id==user.id))
def business_for(db,user):
    active=db.scalars(select(BusinessUser).where(BusinessUser.user_id==user.id)).all()
    if not active: raise HTTPException(400,'No business found')
    return active[0].business_id

def avg_cost(db,bid,pid):
    purchases=db.scalars(select(Purchase).where(Purchase.business_id==bid,Purchase.product_id==pid,Purchase.status=='posted')).all()
    value=Decimal(0); quantity=Decimal(0)
    for x in purchases:
        value += Decimal(x.total); quantity += Decimal(x.quantity)
    return money(value/quantity) if quantity>0 else Decimal(0)

app=FastAPI(title='ReconAI',version='4.0.0')

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
            print(f"Schema create warning (will retry on first request): {e}")

def dbdep():
    with Session(engine) as db: yield db

@app.get('/health')
def health():
    return {'status':'ok','service':'reconai','version':'4.0.0'}
