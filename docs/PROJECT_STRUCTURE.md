# Project Structure

## Directory Layout

```
project/
├── src/                      # Source code
│   ├── __init__.py
│   ├── app.py               # Main application
│   └── setup_db.py          # Database initialization
├── tests/                    # Unit and integration tests
├── config/                   # Configuration files
├── data/                     # Data files and databases
│   ├── pipeline_output.csv
│   └── source_warehouse.db
├── docs/                     # Documentation
├── README.md
├── .env.example             # Environment variables template
└── .gitignore
```

## Directory Descriptions

- **src/**: All application source code
- **tests/**: Test files (unit tests, integration tests)
- **config/**: Configuration settings
- **data/**: Data files, CSVs, databases (typically in .gitignore)
- **docs/**: Project documentation and guides
