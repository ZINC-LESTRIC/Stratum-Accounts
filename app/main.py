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

if AUTO_CREATE:
    Base.metadata.create_all(engine)
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
    # Create the session row first so its id can be embedded in the signed token.
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
    # Payment semantics differ by transaction type. Credit sales create AR;
    # credit purchases/expenses create AP. Never map a liability transaction to AR.
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
    active_id=getattr(user,'_active_business_id',None)
    if active_id is not None:
        b=db.scalar(select(Business).join(BusinessUser,BusinessUser.business_id==Business.id).where(Business.id==active_id,BusinessUser.user_id==user.id))
        if not b: raise HTTPException(403,'You do not have access to this business')
        return b
    b=db.scalar(select(Business).join(BusinessUser,BusinessUser.business_id==Business.id).where(BusinessUser.user_id==user.id).order_by(Business.id))
    if not b: raise HTTPException(400,'Business setup required')
    return b
def require_role(db,bid,user,roles):
    m=membership(db,bid,user)
    if not m or m.role not in roles: raise HTTPException(403,'Insufficient permissions')
    return m

def balances(db,bid,asof=None):
    q=select(Account.name,Account.type,func.sum(JournalLine.debit),func.sum(JournalLine.credit)).join(JournalLine,JournalLine.account_id==Account.id).join(JournalEntry,JournalEntry.id==JournalLine.journal_entry_id).where(Account.business_id==bid,JournalEntry.status=='posted')
    if asof: q=q.where(JournalEntry.txn_date<=asof)
    out={}
    for n,t,d,c in db.execute(q.group_by(Account.id)).all():
        d=Decimal(d or 0); c=Decimal(c or 0); out[n]=money(d-c if t in ('Asset','Expense') else c-d)
    return out

def period_balances(db,bid,start,end):
    q=select(Account.name,Account.type,func.sum(JournalLine.debit),func.sum(JournalLine.credit)).join(JournalLine,JournalLine.account_id==Account.id).join(JournalEntry,JournalEntry.id==JournalLine.journal_entry_id).where(Account.business_id==bid,JournalEntry.status=='posted',JournalEntry.txn_date>=start,JournalEntry.txn_date<=end).group_by(Account.id)
    out={}
    for n,t,d,c in db.execute(q).all():
        d=Decimal(d or 0); c=Decimal(c or 0); out[n]=money(d-c if t in ('Asset','Expense') else c-d)
    return out

def inventory_expected(db,bid,pid,asof):
    p=db.scalar(select(Product).where(Product.id==pid,Product.business_id==bid))
    if not p: raise HTTPException(404,'Product not found')
    total=Decimal(p.opening_quantity or 0)
    rows=db.execute(select(InventoryMovement.movement_type,func.sum(InventoryMovement.quantity)).where(InventoryMovement.business_id==bid,InventoryMovement.product_id==pid,InventoryMovement.txn_date<=asof).group_by(InventoryMovement.movement_type)).all()
    for typ,v in rows:
        v=Decimal(v or 0); total += v if typ in ('opening','purchase','adjustment_in') else -v
    return qty(total)

def inventory_value(db,bid,asof):
    total=Decimal(0)
    for p in db.scalars(select(Product).where(Product.business_id==bid,Product.active==True)).all():
        total += money(inventory_expected(db,bid,p.id,asof)*weighted_average_cost(db,bid,p.id,asof))
    return money(total)

def integrity(db,bid,asof):
    b=balances(db,bid,asof); types={a.name:a.type for a in db.scalars(select(Account).where(Account.business_id==bid)).all()}
    assets=sum((v for n,v in b.items() if types.get(n)=='Asset'),Decimal(0)); liab=sum((v for n,v in b.items() if types.get(n)=='Liability'),Decimal(0)); eq=sum((v for n,v in b.items() if types.get(n)=='Equity'),Decimal(0)); rev=sum((v for n,v in b.items() if types.get(n)=='Revenue'),Decimal(0)); exp=sum((v for n,v in b.items() if types.get(n)=='Expense'),Decimal(0))
    return {'assets':money(assets),'liabilities':money(liab),'equity':money(eq+rev-exp),'balanced':money(assets)==money(liab+eq+rev-exp)}

def journal_for_source(db,bid,source_type,source_id):
    return db.scalar(select(JournalEntry).where(JournalEntry.business_id==bid,JournalEntry.source_type==source_type,JournalEntry.source_id==source_id,JournalEntry.status=='posted'))

def weighted_average_cost(db,bid,pid,asof=None):
    p=db.scalar(select(Product).where(Product.id==pid,Product.business_id==bid));
    if not p: return Decimal(0)
    quantity=Decimal(p.opening_quantity or 0); value=quantity*Decimal(p.unit_cost or 0)
    q=select(Purchase).where(Purchase.business_id==bid,Purchase.product_id==pid,Purchase.status=='posted')
    if asof is not None: q=q.where(Purchase.txn_date<=asof)
    purchases=db.scalars(q.order_by(Purchase.txn_date,Purchase.id)).all()
    for x in purchases:
        value += Decimal(x.total); quantity += Decimal(x.quantity)
    return money(value/quantity) if quantity>0 else Decimal(0)

app=FastAPI(title='ReconAI',version='4.0.0')

# Allow Vercel frontend (and local dev) to call this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS if CORS_ORIGINS != ['*'] else ['*'],
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)

def dbdep():
    with Session(engine) as db: yield db

@app.get('/health')
def health():
    return {'status':'ok','service':'reconai','version':'4.0.0'}

@app.get('/ready')
def ready(db:Session=Depends(dbdep)):
    try:
        db.execute(select(func.count(User.id)))
        return {'status':'ready'}
    except Exception:
        raise HTTPException(503,'Database unavailable')

def current_user(authorization:Optional[str]=Header(None),x_user_email:Optional[str]=Header(None),x_business_id:Optional[str]=Header(None),db:Session=Depends(dbdep)):
    # JWT bearer auth is authoritative. X-User-Email remains only as a local-development compatibility path.
    if authorization and authorization.lower().startswith('bearer '):
        token=authorization.split(' ',1)[1].strip()
        try:
            payload=jwt.decode(token,JWT_SECRET,algorithms=[JWT_ALG]); sid=payload.get('sid'); uid=payload.get('sub')
            st=db.scalar(select(SessionToken).where(SessionToken.id==int(sid),SessionToken.token_hash==token_hash(token)))
            if not st or st.revoked_at or st.expires_at<datetime.utcnow() or st.user_id!=int(uid): raise ValueError()
            u=db.get(User,int(uid))
            if u:
                setattr(u,'_active_business_id',int(x_business_id) if x_business_id and x_business_id.isdigit() else None)
                return u
        except Exception: pass
    if DEV_HEADER_AUTH and x_user_email:
        u=db.scalar(select(User).where(User.email==x_user_email.lower()))
        if u:
            setattr(u,'_active_business_id',int(x_business_id) if x_business_id and x_business_id.isdigit() else None)
            return u
    raise HTTPException(401,'Authentication required')

class Register(BaseModel): email:str; password:str=Field(min_length=8)
class BusinessIn(BaseModel): name:str; business_type:str='Food Stall'; currency:str='PKR'; opening_cash:Decimal=Decimal(0)
class ProductIn(BaseModel): name:str; sku:str=''; unit:str='pcs'; unit_cost:Decimal=Decimal(0); opening_quantity:Decimal=Decimal(0)
class SaleIn(BaseModel): txn_date:date; product_id:int; quantity:Decimal; unit_price:Decimal; discount:Decimal=Decimal(0); payment_method:str; customer:Optional[str]=None; notes:Optional[str]=None; idempotency_key:Optional[str]=None
class PurchaseIn(BaseModel): txn_date:date; supplier:str; product_id:int; quantity:Decimal; unit_cost:Decimal; payment_method:str; invoice_number:str=''; notes:Optional[str]=None; idempotency_key:Optional[str]=None
class ExpenseIn(BaseModel): txn_date:date; category:str; description:str; amount:Decimal; payment_method:str; vendor:Optional[str]=None; reference:Optional[str]=None; notes:Optional[str]=None; idempotency_key:Optional[str]=None
class CashCountIn(BaseModel): count_date:date; actual_cash:Decimal; notes:Optional[str]=None
class InventoryCountIn(BaseModel): product_id:int; count_date:date; actual_qty:Decimal; notes:Optional[str]=None
class BankAccountIn(BaseModel): name:str; account_number_masked:str=''; currency:str='PKR'
class BankTxnIn(BaseModel): bank_account_id:int; txn_date:date; description:str; amount:Decimal; direction:str; reference:str=''
class AskIn(BaseModel): question:str
class ReverseIn(BaseModel): reason:str=Field(min_length=3)
class CustomerIn(BaseModel): name:str; phone:str=''; email:str=''
class SupplierIn(BaseModel): name:str; phone:str=''; email:str=''

@app.get('/')
def root(): return FileResponse('static/index.html')

@app.post('/api/auth/register')
def register(x:Register,db:Session=Depends(dbdep)):
    email=x.email.lower().strip()
    if db.scalar(select(User).where(User.email==email)): raise HTTPException(409,'User already exists')
    u=User(email=email,password_hash=hash_pw(x.password)); db.add(u); db.commit(); token,exp=issue_token(db,u.id); db.commit(); return {'email':u.email,'token':token,'expires_at':exp.isoformat()}

@app.post('/api/auth/login')
def login(x:Register,db:Session=Depends(dbdep)):
    u=db.scalar(select(User).where(User.email==x.email.lower().strip()))
    if not u or not verify_pw(x.password,u.password_hash): raise HTTPException(401,'Invalid credentials')
    token,exp=issue_token(db,u.id); db.commit(); return {'email':u.email,'token':token,'expires_at':exp.isoformat()}

@app.post('/api/auth/logout')
def logout(authorization:Optional[str]=Header(None),db:Session=Depends(dbdep)):
    if authorization and authorization.lower().startswith('bearer '):
        token=authorization.split(' ',1)[1].strip(); st=db.scalar(select(SessionToken).where(SessionToken.token_hash==token_hash(token)))
        if st: st.revoked_at=datetime.utcnow(); db.commit()
    return {'ok':True}

@app.post('/api/business/{business_id}/select')
def select_business(business_id:int,user=Depends(current_user),db:Session=Depends(dbdep)):
    b=db.scalar(select(Business).join(BusinessUser,BusinessUser.business_id==Business.id).where(Business.id==business_id,BusinessUser.user_id==user.id))
    if not b: raise HTTPException(403,'You do not have access to this business')
    return {'id':b.id,'name':b.name,'currency':b.currency}

@app.get('/api/businesses')
def businesses(user=Depends(current_user),db:Session=Depends(dbdep)):
    rows=db.execute(select(Business,BusinessUser.role).join(BusinessUser,BusinessUser.business_id==Business.id).where(BusinessUser.user_id==user.id).order_by(Business.id)).all()
    return [{'id':b.id,'name':b.name,'currency':b.currency,'type':b.business_type,'role':role} for b,role in rows]

@app.post('/api/business/{business_id}/members')
def add_member(business_id:int,email:str,role:str='staff',user=Depends(current_user),db:Session=Depends(dbdep)):
    require_role(db,business_id,user,{'owner','admin'})
    if role not in {'admin','staff'}: raise HTTPException(400,'Role must be admin or staff')
    target=db.scalar(select(User).where(User.email==email.lower().strip()))
    if not target: raise HTTPException(404,'User not found')
    if db.scalar(select(BusinessUser).where(BusinessUser.business_id==business_id,BusinessUser.user_id==target.id)): raise HTTPException(409,'User already belongs to business')
    db.add(BusinessUser(business_id=business_id,user_id=target.id,role=role)); audit(db,business_id,user.id,'add_member','user',target.id,new=role); db.commit(); return {'status':'added'}

@app.get('/api/business/{business_id}/members')
def members(business_id:int,user=Depends(current_user),db:Session=Depends(dbdep)):
    require_role(db,business_id,user,{'owner','admin'})
    rows=db.execute(select(User.email,BusinessUser.user_id,BusinessUser.role).join(BusinessUser,BusinessUser.user_id==User.id).where(BusinessUser.business_id==business_id)).all()
    return [{'user_id':uid,'email':email,'role':role} for email,uid,role in rows]

@app.post('/api/business')
def create_business(x:BusinessIn,user=Depends(current_user),db:Session=Depends(dbdep)):
    if x.opening_cash<0 or not x.name.strip() or not x.currency.strip(): raise HTTPException(400,'Invalid business setup')
    b=Business(name=x.name.strip(),business_type=x.business_type.strip(),currency=x.currency.upper().strip(),owner_id=user.id); db.add(b); db.flush(); db.add(BusinessUser(business_id=b.id,user_id=user.id,role='owner')); seed_accounts(db,b.id)
    if x.opening_cash>0: post_journal(db,b.id,user.id,date.today(),'opening_balance',b.id,'Opening cash',[(acct(db,b.id,'Cash').id,x.opening_cash,0,'Opening cash'),(acct(db,b.id,'Owner Capital').id,0,x.opening_cash,'Owner capital')])
    audit(db,b.id,user.id,'create','business',b.id,new=x.model_dump_json()); db.commit(); return {'id':b.id,'name':b.name,'currency':b.currency}

@app.get('/api/business')
def get_business(user=Depends(current_user),db:Session=Depends(dbdep)):
    b=business_for(db,user); m=membership(db,b.id,user); return {'id':b.id,'name':b.name,'type':b.business_type,'currency':b.currency,'role':m.role}

@app.get('/api/accounts')
def accounts(user=Depends(current_user),db:Session=Depends(dbdep)):
    b=business_for(db,user); return [{'id':a.id,'code':a.code,'name':a.name,'type':a.type,'normal_balance':a.normal_balance} for a in db.scalars(select(Account).where(Account.business_id==b.id,Account.active==True).order_by(Account.code)).all()]

@app.post('/api/products')
def create_product(x:ProductIn,user=Depends(current_user),db:Session=Depends(dbdep)):
    b=business_for(db,user); require_role(db,b.id,user,{'owner','admin'})
    if x.unit_cost<0 or x.opening_quantity<0: raise HTTPException(400,'Invalid product values')
    if x.sku and db.scalar(select(Product).where(Product.business_id==b.id,Product.sku==x.sku)): raise HTTPException(409,'SKU already exists')
    p=Product(business_id=b.id,name=x.name.strip(),sku=x.sku.strip(),unit=x.unit.strip(),unit_cost=money(x.unit_cost),opening_quantity=qty(x.opening_quantity),active=True); db.add(p); db.flush()
    if x.opening_quantity>0 and x.unit_cost<=0: raise HTTPException(400,'Opening quantity requires a positive opening unit cost')
    if x.opening_quantity>0:
        amount=money(x.opening_quantity*x.unit_cost)
        post_journal(db,b.id,user.id,date.today(),'opening_inventory',p.id,f'Opening inventory: {p.name}',[(acct(db,b.id,'Inventory').id,amount,0,'Opening inventory'),(acct(db,b.id,'Owner Capital').id,0,amount,'Opening inventory capital')])
        db.add(InventoryMovement(business_id=b.id,product_id=p.id,txn_date=date.today(),movement_type='opening',quantity=qty(x.opening_quantity),unit_cost=money(x.unit_cost),source_type='opening_inventory',source_id=p.id))
    audit(db,b.id,user.id,'create','product',p.id,new=json.dumps(x.model_dump(),default=str)); db.commit(); return {'id':p.id,'name':p.name}

@app.get('/api/products')
def products(user=Depends(current_user),db:Session=Depends(dbdep)):
    b=business_for(db,user); return [{'id':p.id,'name':p.name,'sku':p.sku,'unit':p.unit,'unit_cost':str(p.unit_cost),'opening_quantity':str(p.opening_quantity)} for p in db.scalars(select(Product).where(Product.business_id==b.id,Product.active==True)).all()]

@app.post('/api/customers')
def create_customer(x:CustomerIn,user=Depends(current_user),db:Session=Depends(dbdep)):
    b=business_for(db,user); c=Customer(business_id=b.id,**x.model_dump()); db.add(c); db.commit(); return {'id':c.id,'name':c.name}
@app.get('/api/customers')
def customers(user=Depends(current_user),db:Session=Depends(dbdep)):
    b=business_for(db,user); return [{'id':x.id,'name':x.name,'phone':x.phone,'email':x.email} for x in db.scalars(select(Customer).where(Customer.business_id==b.id,Customer.active==True)).all()]
@app.post('/api/suppliers')
def create_supplier(x:SupplierIn,user=Depends(current_user),db:Session=Depends(dbdep)):
    b=business_for(db,user); s=Supplier(business_id=b.id,**x.model_dump()); db.add(s); db.commit(); return {'id':s.id,'name':s.name}
@app.get('/api/suppliers')
def suppliers(user=Depends(current_user),db:Session=Depends(dbdep)):
    b=business_for(db,user); return [{'id':x.id,'name':x.name,'phone':x.phone,'email':x.email} for x in db.scalars(select(Supplier).where(Supplier.business_id==b.id,Supplier.active==True)).all()]

@app.post('/api/sales')
def sale(x:SaleIn,user=Depends(current_user),db:Session=Depends(dbdep)):
    b=business_for(db,user); require_role(db,b.id,user,{'owner','admin','staff'})
    if x.idempotency_key:
        old=db.scalar(select(Sale).where(Sale.business_id==b.id,Sale.idempotency_key==x.idempotency_key))
        if old: return {'id':old.id,'total':str(old.total),'cogs':str(old.cogs),'duplicate':True}
    p=db.scalar(select(Product).where(Product.id==x.product_id,Product.business_id==b.id,p.active==True)) if False else db.scalar(select(Product).where(Product.id==x.product_id,Product.business_id==b.id,Product.active==True))
    if not p or x.quantity<=0 or x.unit_price<0 or x.discount<0: raise HTTPException(400,'Invalid sale')
    total=money(x.quantity*x.unit_price-x.discount)
    if total<=0: raise HTTPException(400,'Sale total must be positive')
    available=inventory_expected(db,b.id,p.id,x.txn_date)
    if x.quantity>available: raise HTTPException(400,f'Insufficient inventory. Expected {available} {p.unit}')
    cost=weighted_average_cost(db,b.id,p.id,x.txn_date); cogs=money(x.quantity*cost)
    s=Sale(business_id=b.id,txn_date=x.txn_date,product_id=p.id,quantity=qty(x.quantity),unit_price=money(x.unit_price),discount=money(x.discount),payment_method=x.payment_method,customer=x.customer,notes=x.notes,total=total,cogs=cogs,status='posted',idempotency_key=x.idempotency_key); db.add(s); db.flush()
    pay=payment_account(db,b.id,x.payment_method); lines=[(pay.id,total,0,'Customer payment'),(acct(db,b.id,'Food Sales').id,0,total,'Sales revenue')]
    if cogs>0: lines += [(acct(db,b.id,'Cost of Goods Sold').id,cogs,0,'Cost of goods sold'),(acct(db,b.id,'Inventory').id,0,cogs,'Inventory issued')]
    je=post_journal(db,b.id,user.id,x.txn_date,'sale',s.id,f'Sale of {p.name}',lines); s.journal_entry_id=je.id
    db.add(InventoryMovement(business_id=b.id,product_id=p.id,txn_date=x.txn_date,movement_type='sale',quantity=qty(x.quantity),unit_cost=cost,source_type='sale',source_id=s.id)); audit(db,b.id,user.id,'create','sale',s.id,new=json.dumps({'total':str(total),'cogs':str(cogs)})); db.commit(); return {'id':s.id,'total':str(total),'cogs':str(cogs)}

@app.post('/api/purchases')
def purchase(x:PurchaseIn,user=Depends(current_user),db:Session=Depends(dbdep)):
    b=business_for(db,user); require_role(db,b.id,user,{'owner','admin','staff'})
    if x.idempotency_key:
        old=db.scalar(select(Purchase).where(Purchase.business_id==b.id,Purchase.idempotency_key==x.idempotency_key))
        if old: return {'id':old.id,'total':str(old.total),'new_average_cost':str((db.get(Product,old.product_id)).unit_cost),'duplicate':True}
    p=db.scalar(select(Product).where(Product.id==x.product_id,Product.business_id==b.id,Product.active==True))
    if not p or x.quantity<=0 or x.unit_cost<=0: raise HTTPException(400,'Invalid purchase')
    total=money(x.quantity*x.unit_cost)
    r=Purchase(business_id=b.id,txn_date=x.txn_date,supplier=x.supplier,product_id=p.id,quantity=qty(x.quantity),unit_cost=money(x.unit_cost),total=total,payment_method=x.payment_method,invoice_number=x.invoice_number,notes=x.notes,status='posted',idempotency_key=x.idempotency_key); db.add(r); db.flush()
    pay=payment_account(db,b.id,x.payment_method,'purchase'); je=post_journal(db,b.id,user.id,x.txn_date,'purchase',r.id,f'Purchase from {x.supplier}',[(acct(db,b.id,'Inventory').id,total,0,'Inventory purchase'),(pay.id,0,total,'Payment')]); r.journal_entry_id=je.id
    db.add(InventoryMovement(business_id=b.id,product_id=p.id,txn_date=x.txn_date,movement_type='purchase',quantity=qty(x.quantity),unit_cost=money(x.unit_cost),source_type='purchase',source_id=r.id))
    # Moving-average cost becomes the deterministic current cost basis.
    old_qty=inventory_expected(db,b.id,p.id,x.txn_date)-qty(x.quantity); old_cost=Decimal(p.unit_cost or 0); new_qty=old_qty+qty(x.quantity)
    p.unit_cost=money(((old_qty*old_cost)+(qty(x.quantity)*money(x.unit_cost)))/new_qty) if new_qty>0 else money(x.unit_cost)
    audit(db,b.id,user.id,'create','purchase',r.id,new=json.dumps({'total':str(total),'unit_cost':str(x.unit_cost)})); db.commit(); return {'id':r.id,'total':str(total),'new_average_cost':str(p.unit_cost)}

@app.post('/api/expenses')
def expense(x:ExpenseIn,user=Depends(current_user),db:Session=Depends(dbdep)):
    b=business_for(db,user); require_role(db,b.id,user,{'owner','admin','staff'})
    if x.idempotency_key:
        old=db.scalar(select(Expense).where(Expense.business_id==b.id,Expense.idempotency_key==x.idempotency_key))
        if old: return {'id':old.id,'amount':str(old.amount),'duplicate':True}
    if x.amount<=0: raise HTTPException(400,'Invalid expense')
    mapping={'Rent':'Rent','Electricity':'Utilities','Gas':'Utilities','Salaries':'Salaries','Packaging':'Packaging','Transport':'Transport','Marketing':'Marketing','Repairs':'Repairs'}
    e=Expense(business_id=b.id,txn_date=x.txn_date,category=x.category,description=x.description,amount=money(x.amount),payment_method=x.payment_method,vendor=x.vendor,reference=x.reference,notes=x.notes,status='posted',idempotency_key=x.idempotency_key); db.add(e); db.flush(); ea=acct(db,b.id,mapping.get(x.category,'Other Expenses')); pa=payment_account(db,b.id,x.payment_method,'expense'); je=post_journal(db,b.id,user.id,x.txn_date,'expense',e.id,x.description,[(ea.id,e.amount,0,x.description),(pa.id,0,e.amount,'Payment')]); e.journal_entry_id=je.id; audit(db,b.id,user.id,'create','expense',e.id,new=str(e.amount)); db.commit(); return {'id':e.id,'amount':str(e.amount)}

@app.post('/api/cash-count')
def cash_count(x:CashCountIn,user=Depends(current_user),db:Session=Depends(dbdep)):
    b=business_for(db,user); require_role(db,b.id,user,{'owner','admin','staff'})
    if x.actual_cash<0: raise HTTPException(400,'Invalid cash count')
    c=CashCount(business_id=b.id,count_date=x.count_date,actual_cash=money(x.actual_cash),notes=x.notes); db.add(c); db.flush(); audit(db,b.id,user.id,'create','cash_count',c.id,new=str(c.actual_cash)); db.commit(); return {'id':c.id}

@app.post('/api/inventory-count')
def inv_count(x:InventoryCountIn,user=Depends(current_user),db:Session=Depends(dbdep)):
    b=business_for(db,user); require_role(db,b.id,user,{'owner','admin','staff'}); p=db.scalar(select(Product).where(Product.id==x.product_id,Product.business_id==b.id,Product.active==True))
    if not p or x.actual_qty<0: raise HTTPException(400,'Invalid inventory count')
    expected=inventory_expected(db,b.id,p.id,x.count_date); c=InventoryCount(business_id=b.id,product_id=p.id,count_date=x.count_date,actual_qty=qty(x.actual_qty),notes=x.notes); db.add(c); db.flush(); audit(db,b.id,user.id,'create','inventory_count',c.id,new=str(c.actual_qty)); db.commit(); return {'id':c.id,'expected':str(expected),'difference':str(qty(x.actual_qty)-expected)}


class InventoryAdjustmentIn(BaseModel):
    product_id:int; adjustment_qty:Decimal; adjustment_date:date; reason:str=Field(min_length=3)

@app.post('/api/inventory-adjustments')
def inventory_adjustment(x:InventoryAdjustmentIn,user=Depends(current_user),db:Session=Depends(dbdep)):
    b=business_for(db,user); require_role(db,b.id,user,{'owner','admin'})
    p=db.scalar(select(Product).where(Product.id==x.product_id,Product.business_id==b.id,Product.active==True))
    if not p or x.adjustment_qty==0: raise HTTPException(400,'Invalid inventory adjustment')
    cost=Decimal(p.unit_cost or 0); amount=money(abs(x.adjustment_qty)*cost)
    if amount>0:
        inv=acct(db,b.id,'Inventory'); adj=acct(db,b.id,'Inventory Adjustments')
        lines=[(inv.id,amount,0,'Inventory increase'),(adj.id,0,amount,'Inventory adjustment')]
        if x.adjustment_qty<0: lines=[(adj.id,amount,0,'Inventory shrinkage'),(inv.id,0,amount,'Inventory decrease')]
        je=post_journal(db,b.id,user.id,x.adjustment_date,'inventory_adjustment',None,x.reason,lines)
    else: je=None
    typ='adjustment_in' if x.adjustment_qty>0 else 'adjustment_out'
    db.add(InventoryMovement(business_id=b.id,product_id=p.id,txn_date=x.adjustment_date,movement_type=typ,quantity=qty(abs(x.adjustment_qty)),unit_cost=cost,source_type='inventory_adjustment',source_id=je.id if je else None))
    audit(db,b.id,user.id,'create','inventory_adjustment',je.id if je else 0,new=str(x.adjustment_qty),reason=x.reason); db.commit()
    return {'status':'posted','quantity':str(x.adjustment_qty),'journal_entry_id':je.id if je else None}

@app.post('/api/bank-accounts')
def bank_account(x:BankAccountIn,user=Depends(current_user),db:Session=Depends(dbdep)):
    b=business_for(db,user); require_role(db,b.id,user,{'owner','admin'}); ba=BankAccount(business_id=b.id,name=x.name.strip(),account_number_masked=x.account_number_masked,currency=x.currency.upper()); db.add(ba); db.commit(); return {'id':ba.id,'name':ba.name}
@app.get('/api/bank-accounts')
def bank_accounts(user=Depends(current_user),db:Session=Depends(dbdep)):
    b=business_for(db,user); return [{'id':x.id,'name':x.name,'currency':x.currency} for x in db.scalars(select(BankAccount).where(BankAccount.business_id==b.id,BankAccount.active==True)).all()]
@app.post('/api/bank-transactions')
def bank_txn(x:BankTxnIn,user=Depends(current_user),db:Session=Depends(dbdep)):
    b=business_for(db,user); require_role(db,b.id,user,{'owner','admin','staff'}); ba=db.scalar(select(BankAccount).where(BankAccount.id==x.bank_account_id,BankAccount.business_id==b.id,BankAccount.active==True))
    if not ba or x.amount<=0 or x.direction not in ('debit','credit'): raise HTTPException(400,'Invalid bank transaction')
    t=BankTransaction(business_id=b.id,bank_account_id=ba.id,txn_date=x.txn_date,description=x.description,amount=money(x.amount),direction=x.direction,reference=x.reference,imported=False); db.add(t); db.flush(); audit(db,b.id,user.id,'create','bank_transaction',t.id,new=json.dumps(x.model_dump(),default=str)); db.commit(); return {'id':t.id,'status':t.status}
@app.post('/api/bank-transactions/import')
def bank_import(file:UploadFile=File(...),user=Depends(current_user),db:Session=Depends(dbdep)):
    b=business_for(db,user); require_role(db,b.id,user,{'owner','admin'}); raw=file.file.read()
    try: text=raw.decode('utf-8-sig')
    except UnicodeDecodeError: raise HTTPException(400,'CSV must be UTF-8')
    rows=list(csv.DictReader(io.StringIO(text))); count=0; skipped=0
    for r in rows:
        try:
            ba_id=int(r.get('bank_account_id') or 0); ba=db.scalar(select(BankAccount).where(BankAccount.id==ba_id,BankAccount.business_id==b.id)); direction=r.get('direction','').lower(); amount=money(r.get('amount','0')); d=date.fromisoformat(r['date'])
            if not ba or direction not in ('debit','credit') or amount<=0: raise ValueError()
            duplicate=db.scalar(select(BankTransaction).where(BankTransaction.business_id==b.id,BankTransaction.bank_account_id==ba_id,BankTransaction.txn_date==d,BankTransaction.amount==amount,BankTransaction.reference==r.get('reference',''),BankTransaction.description==r.get('description','')))
            if duplicate: skipped+=1; continue
            db.add(BankTransaction(business_id=b.id,bank_account_id=ba_id,txn_date=d,description=r.get('description',''),amount=amount,direction=direction,reference=r.get('reference',''),imported=True)); count+=1
        except Exception: skipped+=1
    db.commit(); return {'imported':count,'skipped':skipped}

@app.get('/api/bank-reconciliation')
def bank_recon(user=Depends(current_user),db:Session=Depends(dbdep)):
    b=business_for(db,user); out=[]
    entries=db.scalars(select(JournalEntry).where(JournalEntry.business_id==b.id,JournalEntry.status=='posted')).all()
    for t in db.scalars(select(BankTransaction).where(BankTransaction.business_id==b.id).order_by(BankTransaction.txn_date.desc(),BankTransaction.id.desc())).all():
        candidates=[]
        for e in entries:
            if e.id==t.matched_journal_id or abs((e.txn_date-t.txn_date).days)>3: continue
            lines=db.scalars(select(JournalLine).where(JournalLine.journal_entry_id==e.id)).all()
            amount=max(sum((Decimal(l.debit or 0) for l in lines),Decimal(0)),sum((Decimal(l.credit or 0) for l in lines),Decimal(0)))
            # Direction check against the Bank account line.
            bank_lines=[l for l in lines if db.scalar(select(Account).where(Account.id==l.account_id)).name=='Bank']
            if not bank_lines: continue
            bank_dir='credit' if any(Decimal(l.debit or 0)>0 for l in bank_lines) else 'debit'
            bank_amount=max(sum((Decimal(l.debit or 0) for l in bank_lines),Decimal(0)),sum((Decimal(l.credit or 0) for l in bank_lines),Decimal(0)))
            if money(bank_amount)==money(t.amount) and bank_dir==t.direction: candidates.append(e)
        if len(candidates)==1: t.status='matched'; t.matched_journal_id=candidates[0].id
        elif len(candidates)>1: t.status='review_duplicate'
        else:
            # If same date/description but different amount, surface mismatch.
            near=[e for e in entries if abs((e.txn_date-t.txn_date).days)<=3 and t.description.lower()[:12] in e.description.lower()]
            t.status='amount_mismatch' if near else 'unmatched'
        out.append({'id':t.id,'date':str(t.txn_date),'description':t.description,'amount':str(t.amount),'direction':t.direction,'status':t.status,'matched_journal_id':t.matched_journal_id})
    db.commit(); return out

@app.post('/api/bank-transactions/{transaction_id}/match/{journal_id}')
def confirm_bank_match(transaction_id:int,journal_id:int,user=Depends(current_user),db:Session=Depends(dbdep)):
    b=business_for(db,user); require_role(db,b.id,user,{'owner','admin'}); t=db.scalar(select(BankTransaction).where(BankTransaction.id==transaction_id,BankTransaction.business_id==b.id)); j=db.scalar(select(JournalEntry).where(JournalEntry.id==journal_id,JournalEntry.business_id==b.id,JournalEntry.status=='posted'))
    if not t or not j: raise HTTPException(404,'Transaction or journal entry not found')
    t.status='matched'; t.matched_journal_id=j.id; audit(db,b.id,user.id,'match','bank_transaction',t.id,new=str(j.id)); db.commit(); return {'status':'matched'}

@app.get('/api/journal-entries')
def journal(user=Depends(current_user),db:Session=Depends(dbdep)):
    b=business_for(db,user); out=[]
    for e in db.scalars(select(JournalEntry).where(JournalEntry.business_id==b.id).order_by(JournalEntry.id.desc())).all():
        lines=db.scalars(select(JournalLine).where(JournalLine.journal_entry_id==e.id)).all(); debit=sum((Decimal(l.debit) for l in lines),Decimal(0)); credit=sum((Decimal(l.credit) for l in lines),Decimal(0)); out.append({'id':e.id,'date':str(e.txn_date),'description':e.description,'source':e.source_type,'status':e.status,'reversal_of_id':e.reversal_of_id,'balanced':money(debit)==money(credit),'lines':[{'account':db.get(Account,l.account_id).name,'debit':str(l.debit),'credit':str(l.credit)} for l in lines]})
    return out

@app.post('/api/journal-entries/{journal_id}/reverse')
def reverse_journal(journal_id:int,x:ReverseIn,user=Depends(current_user),db:Session=Depends(dbdep)):
    b=business_for(db,user); require_role(db,b.id,user,{'owner','admin'}); j=db.scalar(select(JournalEntry).where(JournalEntry.id==journal_id,JournalEntry.business_id==b.id,JournalEntry.status=='posted'))
    if not j: raise HTTPException(404,'Posted journal entry not found')
    if db.scalar(select(JournalEntry).where(JournalEntry.reversal_of_id==j.id)): raise HTTPException(409,'Journal entry already reversed')
    lines=db.scalars(select(JournalLine).where(JournalLine.journal_entry_id==j.id)).all(); rev=post_journal(db,b.id,user.id,date.today(),'reversal',j.id,f'Reversal: {j.description}',[(l.account_id,l.credit,l.debit,'Reversal') for l in lines]); rev.reversal_of_id=j.id; j.status='reversed'; audit(db,b.id,user.id,'reverse','journal_entry',j.id,new=str(rev.id),reason=x.reason); db.commit(); return {'reversal_journal_id':rev.id,'status':'reversed'}

def inventory_rows(db,bid,asof):
    out=[]
    for p in db.scalars(select(Product).where(Product.business_id==bid,Product.active==True)).all():
        exp=inventory_expected(db,bid,p.id,asof); latest=db.scalar(select(InventoryCount).where(InventoryCount.business_id==bid,InventoryCount.product_id==p.id,InventoryCount.count_date<=asof).order_by(InventoryCount.count_date.desc(),InventoryCount.id.desc())); actual=Decimal(latest.actual_qty) if latest else None; diff=qty(actual-exp) if actual is not None else None; value=money(exp*weighted_average_cost(db,bid,p.id,asof)); out.append({'id':p.id,'name':p.name,'sku':p.sku,'unit':p.unit,'expected':str(exp),'actual':str(actual) if actual is not None else None,'difference':str(diff) if diff is not None else None,'unit_cost':str(p.unit_cost),'value':str(value)})
    return out
@app.get('/api/inventory')
def inventory_endpoint(user=Depends(current_user),db:Session=Depends(dbdep)):
    b=business_for(db,user); return inventory_rows(db,b.id,date.today())


def cash_flow_components(db,bid,start,end):
    operating=Decimal(0); investing=Decimal(0); financing=Decimal(0)
    for j in db.scalars(select(JournalEntry).where(JournalEntry.business_id==bid,JournalEntry.status=='posted',JournalEntry.txn_date>=start,JournalEntry.txn_date<=end)).all():
        lines=db.scalars(select(JournalLine).where(JournalLine.journal_entry_id==j.id)).all()
        cash_lines=[]
        for l in lines:
            a=db.get(Account,l.account_id)
            if a and a.name in ('Cash','Bank'):
                cash_lines.append((Decimal(l.debit or 0)-Decimal(l.credit or 0), j.source_type))
        for delta,src in cash_lines:
            if src in ('opening_balance','reversal'): financing += delta
            elif src in ('inventory_purchase','purchase','expense','sale'): operating += delta
            else: operating += delta
    return money(operating),money(investing),money(financing)

@app.get('/api/financials')
def financials(start:date,end:date,user=Depends(current_user),db:Session=Depends(dbdep)):
    if end<start: raise HTTPException(400,'Invalid period')
    b=business_for(db,user); pb=period_balances(db,b.id,start,end); allb=balances(db,b.id,end); rev=money(pb.get('Food Sales',0)+pb.get('Other Revenue',0)); cogs=money(pb.get('Cost of Goods Sold',0)); op=money(sum((v for n,v in pb.items() if n in {'Rent','Utilities','Salaries','Packaging','Transport','Marketing','Repairs','Other Expenses'}),Decimal(0))); net=money(rev-cogs-op); bs=integrity(db,b.id,end)
    assets={n:str(v) for n,v in allb.items() if n in {'Cash','Bank','Accounts Receivable','Inventory','Equipment'}}; liabilities={n:str(v) for n,v in allb.items() if n in {'Accounts Payable','Loans'}}
    current_equity=money(allb.get('Owner Capital',0)+allb.get('Retained Earnings',0)+allb.get('Food Sales',0)+allb.get('Other Revenue',0)-allb.get('Cost of Goods Sold',0)-sum((v for n,v in allb.items() if n in {'Rent','Utilities','Salaries','Packaging','Transport','Marketing','Repairs','Other Expenses','Inventory Adjustments'}),Decimal(0))-allb.get('Owner Drawings',0))
    cash_end=money(allb.get('Cash',0)+allb.get('Bank',0)); opening=balances(db,b.id,start-timedelta(days=1)); opening_cash=money(opening.get('Cash',0)+opening.get('Bank',0)); net_change=money(cash_end-opening_cash); ocf,icf,fcf=cash_flow_components(db,b.id,start,end)
    return {'period':{'start':str(start),'end':str(end)},'profit_loss':{'revenue':str(rev),'cogs':str(cogs),'gross_profit':str(rev-cogs),'operating_expenses':str(op),'net_profit':str(net)},'balance_sheet':{'assets':assets,'liabilities':liabilities,'equity':{'Owner Capital':str(allb.get('Owner Capital',0)),'Retained Earnings':str(allb.get('Retained Earnings',0)),'Current Earnings':str(money(current_equity-allb.get('Owner Capital',0)-allb.get('Retained Earnings',0)+allb.get('Owner Drawings',0))),'Owner Drawings':str(allb.get('Owner Drawings',0)),'Total Equity':str(current_equity)},'integrity':bs},'cash_flow':{'opening_cash':str(opening_cash),'net_change_in_cash':str(net_change),'closing_cash':str(cash_end),'operating_cash_flow':str(ocf),'investing_cash_flow':str(icf),'financing_cash_flow':str(fcf)}}

@app.get('/api/reconciliation')
def reconciliation(user=Depends(current_user),db:Session=Depends(dbdep)):
    b=business_for(db,user); today=date.today(); bal=balances(db,b.id,today); expected=bal.get('Cash',Decimal(0)); latest=db.scalar(select(CashCount).where(CashCount.business_id==b.id,CashCount.count_date<=today).order_by(CashCount.count_date.desc(),CashCount.id.desc())); actual=latest.actual_cash if latest else None; diff=money(actual-expected) if actual is not None else None
    invs=inventory_rows(db,b.id,today); invdiff=sum((Decimal(x['difference']) for x in invs if x['difference'] is not None),Decimal(0)); invstatus='not_counted' if not invs or all(x['difference'] is None for x in invs) else ('reconciled' if invdiff==0 else 'discrepancy')
    bank_unmatched=db.scalar(select(func.count(BankTransaction.id)).where(BankTransaction.business_id==b.id,BankTransaction.status!='matched')) or 0
    return {'cash':{'expected':str(expected),'actual':str(actual) if actual is not None else None,'difference':str(diff) if diff is not None else None,'status':'reconciled' if diff==0 else ('discrepancy' if diff is not None else 'not_counted')},'bank':{'status':'reconciled' if bank_unmatched==0 else 'review','unmatched':bank_unmatched},'inventory':{'status':invstatus,'difference':str(invdiff)},'purchases':{'status':'review','unmatched':db.scalar(select(func.count(Purchase.id)).where(Purchase.business_id==b.id,Purchase.status=='posted')) or 0},'sales':{'status':'review','unmatched':db.scalar(select(func.count(Sale.id)).where(Sale.business_id==b.id,Sale.status=='posted')) or 0}}

@app.get('/api/anomalies')
def anomalies(user=Depends(current_user),db:Session=Depends(dbdep)):
    b=business_for(db,user); out=[]
    expenses=db.scalars(select(Expense).where(Expense.business_id==b.id,Expense.status=='posted')).all(); bycat={}
    for e in expenses: bycat.setdefault(e.category,[]).append(Decimal(e.amount))
    for e in expenses:
        hist=[v for v in bycat[e.category] if v!=e.amount]
        if len(hist)>=2:
            avg=sum(hist,Decimal(0))/len(hist)
            if e.amount>avg*Decimal('1.75'): out.append({'type':'large_expense','severity':'high','record_id':e.id,'message':f'{e.category} expense Rs {e.amount} is unusually high versus historical average Rs {money(avg)}'})
    sales=db.scalars(select(Sale).where(Sale.business_id==b.id,Sale.status=='posted')).all(); seen=set()
    for s in sales:
        key=(s.txn_date,s.product_id,s.quantity,s.total,s.payment_method)
        if key in seen: out.append({'type':'duplicate_sale','severity':'medium','record_id':s.id,'message':'Sale matches another sale on date, product, quantity, total and payment method.'})
        seen.add(key)
    for p in db.scalars(select(Product).where(Product.business_id==b.id)).all():
        ps=db.scalars(select(Purchase).where(Purchase.business_id==b.id,Purchase.product_id==p.id,Purchase.status=='posted').order_by(Purchase.txn_date,Purchase.id)).all(); costs=[Decimal(x.unit_cost) for x in ps]
        if len(costs)>=3:
            avg=sum(costs[:-1],Decimal(0))/len(costs[:-1]); latest=costs[-1]
            if latest>avg*Decimal('1.4'): out.append({'type':'supplier_price','severity':'medium','record_id':p.id,'message':f'{p.name} purchase price Rs {latest} is above historical average Rs {money(avg)}'})
    return out

@app.get('/api/audit-logs')
def audit_logs(user=Depends(current_user),db:Session=Depends(dbdep)):
    b=business_for(db,user); return [{'timestamp':x.created_at.isoformat(),'action':x.action,'record_type':x.record_type,'record_id':x.record_id,'reason':x.reason,'original_value':x.original_value,'new_value':x.new_value} for x in db.scalars(select(AuditLog).where(AuditLog.business_id==b.id).order_by(AuditLog.id.desc()).limit(500)).all()]

@app.get('/api/dashboard')
def dashboard(user=Depends(current_user),db:Session=Depends(dbdep)):
    b=business_for(db,user); today=date.today(); pb=period_balances(db,b.id,today,today); bal=balances(db,b.id,today); rev=money(pb.get('Food Sales',0)+pb.get('Other Revenue',0)); cogs=money(pb.get('Cost of Goods Sold',0)); expenses=money(sum((v for n,v in pb.items() if n in {'Rent','Utilities','Salaries','Packaging','Transport','Marketing','Repairs','Other Expenses'}),Decimal(0))); invvalue=inventory_value(db,b.id,today); integ=integrity(db,b.id,today); rec=reconciliation(user,db); return {'business':b.name,'currency':b.currency,'today':{'sales':str(rev),'expenses':str(expenses),'gross_profit':str(rev-cogs),'net_profit':str(rev-cogs-expenses)},'cash_on_hand':str(bal.get('Cash',0)),'bank_balance':str(bal.get('Bank',0)),'inventory_value':str(invvalue),'accounting_equation_ok':integ['balanced'],'reconciliation':rec}

# ---------- read-only AI analyst ----------
def verified_context(db, bid, today, user=None):
    """Build a structured snapshot of verified accounting data for the AI.
    Never includes write capability or speculative numbers.
    """
    pb = period_balances(db, bid, today, today)
    bal = balances(db, bid, today)
    inv = inventory_rows(db, bid, today)
    integ = integrity(db, bid, today)
    # Reconciliation snapshot (cash/inventory/bank)
    expected_cash = bal.get('Cash', Decimal(0))
    latest_cash = db.scalar(
        select(CashCount).where(CashCount.business_id == bid, CashCount.count_date <= today)
        .order_by(CashCount.count_date.desc(), CashCount.id.desc())
    )
    actual_cash = latest_cash.actual_cash if latest_cash else None
    cash_diff = money(actual_cash - expected_cash) if actual_cash is not None else None
    inv_diff = sum((Decimal(x['difference']) for x in inv if x.get('difference') is not None), Decimal(0))
    bank_unmatched = db.scalar(
        select(func.count(BankTransaction.id)).where(
            BankTransaction.business_id == bid, BankTransaction.status != 'matched'
        )
    ) or 0
    anoms = []
    try:
        # Re-use anomalies endpoint logic if available; keep light to avoid recursion
        anoms = anomalies(user, db) if user is not None else []
    except Exception:
        anoms = []
    return {
        'date': str(today),
        'balances': {k: str(v) for k, v in bal.items()},
        'today': {k: str(v) for k, v in pb.items()},
        'inventory': inv,
        'integrity': integ,
        'cash_reconciliation': {
            'expected': str(expected_cash),
            'actual': str(actual_cash) if actual_cash is not None else None,
            'difference': str(cash_diff) if cash_diff is not None else None,
            'status': 'reconciled' if cash_diff == 0 else ('discrepancy' if cash_diff is not None else 'not_counted'),
        },
        'inventory_reconciliation': {
            'total_difference': str(inv_diff),
            'status': 'reconciled' if inv_diff == 0 and any(x.get('difference') is not None for x in inv) else (
                'discrepancy' if inv_diff != 0 else 'not_counted'
            ),
        },
        'bank_unmatched': bank_unmatched,
        'anomalies': anoms,
        'accounts_payable': str(bal.get('Accounts Payable', Decimal(0))),
        'accounts_receivable': str(bal.get('Accounts Receivable', Decimal(0))),
    }

@app.post('/api/ai/ask')
def ai_ask(x: AskIn, user=Depends(current_user), db: Session = Depends(dbdep)):
    b = business_for(db, user)
    today = date.today()
    q = x.question.lower()
    ctx = verified_context(db, b.id, today, user=user)
    facts = []

    # Cash
    if any(w in q for w in ('cash', 'where did my cash go', 'short', 'shortage', 'discrepancy')):
        cr = ctx['cash_reconciliation']
        facts.append(
            f"FACT: Expected cash is Rs {cr['expected']}; physical cash is Rs {cr['actual'] or 'not counted'}; "
            f"difference is Rs {cr['difference'] or 'n/a'}; status={cr['status']}."
        )

    # Inventory
    if 'inventory' in q or 'stock' in q:
        facts.append('FACT: Verified inventory records: ' + json.dumps(ctx['inventory']))
        facts.append(
            f"FACT: Inventory reconciliation total difference = Rs {ctx['inventory_reconciliation']['total_difference']} "
            f"(status={ctx['inventory_reconciliation']['status']})."
        )

    # Anomalies
    if any(w in q for w in ('unusual', 'anomal', 'strange', 'suspicious')):
        facts.append('FACT: Deterministic anomaly flags: ' + json.dumps(ctx.get('anomalies') or []))

    # Profit / sales
    if any(w in q for w in ('profit', 'sold', 'sales', 'revenue', 'margin')):
        pb = ctx['today']
        sales = money(Decimal(pb.get('Food Sales', '0')) + Decimal(pb.get('Other Revenue', '0')))
        cogs = money(Decimal(pb.get('Cost of Goods Sold', '0')))
        facts.append(f"FACT: Today sales are Rs {sales}; COGS is Rs {cogs}; gross profit is Rs {money(sales - cogs)}.")

    # Payables / suppliers
    if 'supplier' in q or 'owe' in q or 'payable' in q:
        facts.append(f"FACT: Accounts Payable balance is Rs {ctx.get('accounts_payable', '0.00')}.")

    # Receivables
    if 'customer' in q or 'receivable' in q or 'owe me' in q:
        facts.append(f"FACT: Accounts Receivable balance is Rs {ctx.get('accounts_receivable', '0.00')}.")

    # Bank
    if 'bank' in q:
        facts.append(f"FACT: Unmatched bank transactions: {ctx.get('bank_unmatched', 0)}.")

    # Integrity
    if 'balance' in q or 'integrity' in q or 'equation' in q:
        integ = ctx.get('integrity') or {}
        facts.append(
            f"FACT: Accounting equation balanced={integ.get('balanced')}; "
            f"assets={integ.get('assets')}, liabilities={integ.get('liabilities')}, equity={integ.get('equity')}."
        )

    if not facts:
        facts.append(
            'FACT: I can answer from verified cash, inventory, sales, profit, reconciliation, '
            'supplier-payable, receivables, bank matching and anomaly data.'
        )

    base_answer = (
        '\n'.join(facts)
        + '\n\nPOSSIBLE EXPLANATION:\n'
        'I will not state a cause unless verified records establish it. '
        'AI suggestions are read-only and cannot modify accounting records.'
    )

    # Optional LLM enhancement. The model receives verified context only and has no tool/write access.
    answer = base_answer
    llm_used = False
    api_key = os.getenv('LLM_API_KEY')
    base_url = os.getenv('LLM_BASE_URL', 'https://api.openai.com/v1/chat/completions')
    model = os.getenv('LLM_MODEL', 'gpt-4.1-mini')
    if api_key:
        try:
            payload = json.dumps({
                'model': model,
                'temperature': 0,
                'messages': [
                    {
                        'role': 'system',
                        'content': (
                            'You are ReconAI Analyst. Use ONLY the verified JSON supplied. '
                            'Clearly label FACT and POSSIBLE EXPLANATION. '
                            'Never invent transactions, balances, or numbers. '
                            'Never claim that you changed or can change accounting records.'
                        ),
                    },
                    {
                        'role': 'user',
                        'content': json.dumps({
                            'question': x.question,
                            'verified': ctx,
                            'deterministic_facts': facts,
                        }),
                    },
                ],
            }).encode()
            req = urllib.request.Request(
                base_url,
                data=payload,
                headers={'Authorization': 'Bearer ' + api_key, 'Content-Type': 'application/json'},
                method='POST',
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode())
            answer = data['choices'][0]['message']['content']
            llm_used = True
        except Exception:
            answer = base_answer

    return {
        'answer': answer,
        'read_only': True,
        'llm_used': llm_used,
        'source': 'verified deterministic accounting backend',
    }
