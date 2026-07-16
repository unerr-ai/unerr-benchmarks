# swebench-registry image: stock CNCF Distribution (registry:2) + our Tigris(S3)-
# backed pull-through config.yml, plus a thin entrypoint that maps Fly's AWS_*
# Tigris secrets onto the driver's REGISTRY_STORAGE_S3_* env. See config.yml for
# why S3 (one shared cache across 2 machines; fly volumes are single-attach).
FROM registry:2
COPY config.yml        /etc/docker/registry/config.yml
COPY reg-entrypoint.sh /reg-entrypoint.sh
RUN chmod +x /reg-entrypoint.sh
# reg-entrypoint.sh execs `registry serve "$@"`; CMD is the config path, exactly
# how the stock image invokes it.
ENTRYPOINT ["/reg-entrypoint.sh"]
CMD ["/etc/docker/registry/config.yml"]
