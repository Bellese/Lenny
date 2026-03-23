FROM curlimages/curl:latest

COPY patient-bundle.json /seed/patient-bundle.json
COPY measure-bundle.json /seed/measure-bundle.json
COPY load-seed-data.sh /seed/load-seed-data.sh

ENTRYPOINT ["sh", "/seed/load-seed-data.sh"]
