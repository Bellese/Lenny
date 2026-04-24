# hapi-cdr-seeded.Dockerfile
#
# Extends hapiproject/hapi:v8.8.0-1 with patient seed data baked into the H2
# database at image build time.  Environment matches the hapi-fhir-cdr service
# in docker-compose.test.yml.
#
# The RUN /seed-hapi.sh step starts HAPI, loads patient-bundle.json (SEED_TYPE=cdr),
# triggers $reindex, then stops HAPI cleanly so H2 flushes to disk.  The resulting
# H2 file lives in the image layer at /data/hapi/h2.mv.db.
#
# IMPORTANT: When running this image, do NOT mount a volume over /data/hapi — that
# would shadow the baked H2 files.  Remove or rename the test_cdrdata volume mount
# in the wiring PR.

FROM hapiproject/hapi:v8.8.0-1

USER root

# Install runtime tools needed by seed-hapi.sh (curl, jq)
RUN apt-get update -qq && \
    apt-get install -y --no-install-recommends curl jq && \
    rm -rf /var/lib/apt/lists/*

# Copy seed bundles and build-time seed script
COPY seed/patient-bundle.json /seed/patient-bundle.json
COPY docker/seed-hapi.sh /seed-hapi.sh
RUN chmod +x /seed-hapi.sh

# Environment from docker-compose.test.yml hapi-fhir-cdr service
ENV hapi.fhir.implementationguides.qicore.name=hl7.fhir.us.qicore
ENV hapi.fhir.implementationguides.qicore.version=6.0.0
ENV hapi.fhir.implementationguides.uscore.name=hl7.fhir.us.core
ENV hapi.fhir.implementationguides.uscore.version=6.1.0
ENV hapi.fhir.implementationguides.cql.name=hl7.fhir.uv.cql
ENV hapi.fhir.implementationguides.cql.version=1.0.0
ENV hapi.fhir.server_address=http://localhost:8180/fhir
ENV hapi.fhir.defer_indexing_for_codesystems_of_size=0
ENV hapi.fhir.client_id_strategy=ANY
ENV hapi.fhir.allow_external_references=true
ENV hapi.fhir.enforce_referential_integrity_on_write=false
ENV hapi.fhir.maximum_expansion_size=50000
ENV spring.datasource.url=jdbc:h2:file:/data/hapi/h2;DB_CLOSE_ON_EXIT=FALSE
ENV spring.datasource.driverClassName=org.h2.Driver

# Bake: start HAPI, load patient seed data, wait for reindex, stop cleanly.
# H2 data file is written to /data/hapi/h2.mv.db inside this image layer.
ENV SEED_TYPE=cdr
RUN /seed-hapi.sh
