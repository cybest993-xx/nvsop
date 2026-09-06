"""How `device` reaches PostgreSQL and HTTP. Nothing above the seam imports this package.

An `import-linter` contract holds that: `device.model`, `device.usecases` and the
`Protocol`s in `device.repository` may not import anything here, nor SQLAlchemy, nor
FastAPI. It is what keeps the use-case suite able to pass in-memory stand-ins at the same
seam these adapters sit at.
"""
