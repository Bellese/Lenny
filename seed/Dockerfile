FROM python:3.11-slim

RUN pip install --no-cache-dir httpx

COPY patient-bundle.json /seed/patient-bundle.json
COPY measure-bundle.json /seed/measure-bundle.json
COPY __init__.py /seed/__init__.py
COPY load_seed_data.py /seed/load_seed_data.py

ENTRYPOINT ["python3", "/seed/load_seed_data.py"]
