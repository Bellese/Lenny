# hapi-measure-seeded.Dockerfile
#
# Extends hapiproject/hapi:v8.8.0-1 with measure + patient seed data baked into
# the H2 database and Lucene index at image build time.  Environment matches the
# hapi-fhir-measure service in docker-compose.test.yml.
#
# The RUN /seed-hapi.sh step starts HAPI, loads measure-bundle.json then
# patient-bundle.json (SEED_TYPE=measure), triggers $reindex, polls ValueSet
# pre-expansion, then stops HAPI cleanly so H2 and Lucene flush to disk.
#
# IMPORTANT: When running this image, do NOT mount a volume over /data/hapi — that
# would shadow the baked H2 and Lucene index files.  Remove or rename the
# test_measuredata volume mount in the wiring PR.

FROM hapiproject/hapi:v8.8.0-1

USER root

# Install runtime tools needed by seed-hapi.sh (curl, jq)
RUN apt-get update -qq && \
    apt-get install -y --no-install-recommends curl jq && \
    rm -rf /var/lib/apt/lists/*

# Copy seed bundles and build-time seed script
COPY seed/measure-bundle.json /seed/measure-bundle.json
COPY seed/patient-bundle.json /seed/patient-bundle.json
COPY docker/seed-hapi.sh /seed-hapi.sh
RUN chmod +x /seed-hapi.sh

# Environment from docker-compose.test.yml hapi-fhir-measure service
ENV hapi.fhir.implementationguides.qicore.name=hl7.fhir.us.qicore
ENV hapi.fhir.implementationguides.qicore.version=6.0.0
ENV hapi.fhir.implementationguides.uscore.name=hl7.fhir.us.core
ENV hapi.fhir.implementationguides.uscore.version=6.1.0
ENV hapi.fhir.implementationguides.cql.name=hl7.fhir.uv.cql
ENV hapi.fhir.implementationguides.cql.version=1.0.0
ENV hapi.fhir.server_address=http://localhost:8181/fhir
ENV hapi.fhir.defer_indexing_for_codesystems_of_size=0
ENV hapi.fhir.client_id_strategy=ANY
ENV hapi.fhir.allow_multiple_delete=true
ENV hapi.fhir.cr.enabled=true
ENV hapi.fhir.allow_external_references=true
ENV hapi.fhir.enforce_referential_integrity_on_write=false
ENV hapi.fhir.maximum_expansion_size=50000
ENV hapi.fhir.reuse_cached_search_results_millis=0
ENV spring.datasource.url=jdbc:h2:file:/data/hapi/h2;DB_CLOSE_ON_EXIT=FALSE
ENV spring.datasource.driverClassName=org.h2.Driver
ENV spring.jpa.properties.hibernate.search.enabled=true
ENV spring.jpa.properties.hibernate.search.backend.type=lucene
ENV spring.jpa.properties.hibernate.search.backend.analysis.configurer=ca.uhn.fhir.jpa.search.HapiHSearchAnalysisConfigurers$$HapiLuceneAnalysisConfigurer
ENV spring.jpa.properties.hibernate.search.backend.directory.root=/data/hapi/lucene
ENV spring.jpa.properties.hibernate.search.backend.lucene_version=LUCENE_CURRENT
ENV spring.jpa.properties.hibernate.search.backend.io.refresh_interval=100

# Bake: start HAPI, load measure + patient seed data, wait for reindex and
# ValueSet pre-expansion, then stop cleanly so H2 and Lucene flush to disk.
ENV SEED_TYPE=measure
RUN /seed-hapi.sh
