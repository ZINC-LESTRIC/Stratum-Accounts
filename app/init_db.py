"""Initialize the configured database schema for a fresh deployment."""
from .main import Base, engine

Base.metadata.create_all(engine)
print('ReconAI database schema initialized.')
