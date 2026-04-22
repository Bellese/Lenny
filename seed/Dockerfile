FROM python:3.11-slim

RUN pip install --no-cache-dir httpx

COPY patient-bundle.json /seed/patient-bundle.json
COPY measure-bundle.json /seed/measure-bundle.json
COPY load-seed-data.py /seed/load-seed-data.py

ENTRYPOINT ["python3", "/seed/load-seed-data.py"]
