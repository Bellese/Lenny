# hapi-cdr-seeded.Dockerfile
#
# Configuration-only layer on top of hapiproject/hapi:v8.8.0-1.
# The seeded H2 database is baked in by the bake-hapi-image.yml workflow via
# "docker run → seed from runner → docker stop → docker commit", NOT by a
# Dockerfile RUN step (the base image is distroless — no shell available).
#
# When running this image, do NOT mount a volume over /data/hapi — that would
# shadow the baked H2 files committed by the bake workflow.
# The wiring PR must remove the test_cdrdata volume mount from docker-compose.test.yml.

FROM hapiproject/hapi:v8.8.0-1

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
ENV spring.jpa.properties.hibernate.search.indexing.plan.synchronization.strategy=sync
