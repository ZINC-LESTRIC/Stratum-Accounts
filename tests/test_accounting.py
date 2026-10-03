from decimal import Decimal
from datetime import date
import uuid
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
import app.main as m
from app.main import app, Base, money, User, Account, JournalEntry, JournalLine

client = TestClient(app)

def setup_client(tmp_path, monkeypatch):
    dbfile = tmp_path / f"{uuid.uuid4().hex}.db"
    eng = create_engine('sqlite:///' + str(dbfile), connect_args={'check_same_thread': False})
    Base.metadata.create_all(eng)
    monkeypatch.setattr(m, 'engine', eng)
    return eng

def register_and_business(name='Kashmir Corndog'):
    email=f'{uuid.uuid4().hex}@test.local'; password='password123'
    r=client.post('/api/auth/register',json={'email':email,'password':password}); assert r.status_code==200, r.text
    token=r.json()['token']; h={'Authorization':'Bearer '+token}
    r=client.post('/api/business',headers=h,json={'name':name,'business_type':'Food Stall','currency':'PKR','opening_cash':100000}); assert r.status_code==200, r.text
    return h,email

def test_critical_cash_flow(tmp_path, monkeypatch):
    setup_client(tmp_path, monkeypatch); h,_=register_and_business()
    p=client.post('/api/products',headers=h,json={'name':'Corndog','sku':'CD-001','unit':'pcs','unit_cost':100,'opening_quantity':1000}).json()
    for payload in [
        {'txn_date':str(date.today()),'supplier':'Ingredients','product_id':p['id'],'quantity':200,'unit_cost':100,'payment_method':'Cash'},
        {'txn_date':str(date.today()),'supplier':'Packaging','product_id':p['id'],'quantity':50,'unit_cost':100,'payment_method':'Cash'},
    ]:
        r=client.post('/api/purchases',headers=h,json=payload); assert r.status_code==200,r.text
    r=client.post('/api/expenses',headers=h,json={'txn_date':str(date.today()),'category':'Rent','description':'Rent','amount':5000,'payment_method':'Cash'}); assert r.status_code==200,r.text
    r=client.post('/api/sales',headers=h,json={'txn_date':str(date.today()),'product_id':p['id'],'quantity':100,'unit_price':350,'discount':0,'payment_method':'Cash'}); assert r.status_code==200,r.text
    r=client.post('/api/cash-count',headers=h,json={'count_date':str(date.today()),'actual_cash':103500}); assert r.status_code==200,r.text
    rec=client.get('/api/reconciliation',headers=h).json()['cash']
    assert rec['expected']=='105000.00'; assert rec['difference']=='-1500.00'; assert rec['status']=='discrepancy'
    js=client.get('/api/journal-entries',headers=h).json(); assert all(x['balanced'] for x in js)
    fin=client.get('/api/financials',headers=h,params={'start':str(date.today()),'end':str(date.today())}).json()
    assert fin['balance_sheet']['integrity']['balanced'] is True

def test_credit_purchase_and_expense_use_accounts_payable(tmp_path, monkeypatch):
    setup_client(tmp_path, monkeypatch); h,_=register_and_business('Credit Test')
    p=client.post('/api/products',headers=h,json={'name':'Oil','sku':'OIL','unit_cost':100,'opening_quantity':10}).json()
    r=client.post('/api/purchases',headers=h,json={'txn_date':str(date.today()),'supplier':'ABC','product_id':p['id'],'quantity':10,'unit_cost':200,'payment_method':'Credit'}); assert r.status_code==200,r.text
    r=client.post('/api/expenses',headers=h,json={'txn_date':str(date.today()),'category':'Rent','description':'Rent','amount':1000,'payment_method':'Credit'}); assert r.status_code==200,r.text
    j=client.get('/api/journal-entries',headers=h).json()
    names=[line['account'] for e in j for line in e['lines'] if e['source'] in ('purchase','expense')]
    assert 'Accounts Payable' in names
    assert 'Accounts Receivable' not in names

def test_opening_inventory_is_accounted(tmp_path, monkeypatch):
    setup_client(tmp_path, monkeypatch); h,_=register_and_business('Inventory Opening')
    r=client.post('/api/products',headers=h,json={'name':'Sausage','sku':'S','unit_cost':50,'opening_quantity':100}); assert r.status_code==200,r.text
    fin=client.get('/api/financials',headers=h,params={'start':str(date.today()),'end':str(date.today())}).json()
    assert fin['balance_sheet']['assets']['Inventory']=='5000.00'
    assert fin['balance_sheet']['integrity']['balanced'] is True

def test_jwt_revocation(tmp_path, monkeypatch):
    setup_client(tmp_path, monkeypatch)
    email=f'{uuid.uuid4().hex}@test.local'; pw='password123'
    r=client.post('/api/auth/register',json={'email':email,'password':pw}); assert r.status_code==200
    h={'Authorization':'Bearer '+r.json()['token']}
    assert client.get('/health').status_code==200
    assert client.post('/api/auth/logout',headers=h).json()['ok'] is True
    assert client.get('/api/business',headers=h).status_code==401

def test_business_isolation(tmp_path, monkeypatch):
    setup_client(tmp_path, monkeypatch)
    h1,_=register_and_business('Biz One')
    r=client.post('/api/products',headers=h1,json={'name':'Private','sku':'P','unit_cost':10,'opening_quantity':10}); assert r.status_code==200
    h2,_=register_and_business('Biz Two')
    r=client.get('/api/products',headers=h2); assert r.status_code==200
    assert all(p['name']!='Private' for p in r.json())

def test_journal_reversal(tmp_path, monkeypatch):
    setup_client(tmp_path, monkeypatch); h,_=register_and_business()
    p=client.post('/api/products',headers=h,json={'name':'Item','sku':'I','unit_cost':10,'opening_quantity':10}).json()
    r=client.post('/api/sales',headers=h,json={'txn_date':str(date.today()),'product_id':p['id'],'quantity':1,'unit_price':100,'discount':0,'payment_method':'Cash'}); assert r.status_code==200
    js=client.get('/api/journal-entries',headers=h).json()
    je=[x for x in js if x['source']=='sale'][0]
    r=client.post(f"/api/journal-entries/{je['id']}/reverse",headers=h); assert r.status_code==200
    js2=client.get('/api/journal-entries',headers=h).json()
    assert any(x.get('reversed_of')==je['id'] for x in js2)

def test_idempotency(tmp_path, monkeypatch):
    setup_client(tmp_path, monkeypatch); h,_=register_and_business()
    p=client.post('/api/products',headers=h,json={'name':'Item','sku':'I','unit_cost':10,'opening_quantity':10}).json()
    key=str(uuid.uuid4())
    payload={'txn_date':str(date.today()),'product_id':p['id'],'quantity':1,'unit_price':50,'discount':0,'payment_method':'Cash'}
    r1=client.post('/api/sales',headers={**h,'Idempotency-Key':key},json=payload); assert r1.status_code==200
    r2=client.post('/api/sales',headers={**h,'Idempotency-Key':key},json=payload); assert r2.status_code==200
    assert r1.json()['id']==r2.json()['id']

def test_accounting_integrity(tmp_path, monkeypatch):
    setup_client(tmp_path, monkeypatch); h,_=register_and_business()
    p=client.post('/api/products',headers=h,json={'name':'X','sku':'X','unit_cost':5,'opening_quantity':20}).json()
    client.post('/api/sales',headers=h,json={'txn_date':str(date.today()),'product_id':p['id'],'quantity':2,'unit_price':30,'discount':0,'payment_method':'Cash'})
    fin=client.get('/api/financials',headers=h,params={'start':str(date.today()),'end':str(date.today())}).json()
    assert fin['balance_sheet']['integrity']['balanced'] is True
    assert fin['pnl']['gross_profit'] is not None
