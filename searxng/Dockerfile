FROM searxng/searxng:latest

# Bake settings.yml directly into the image so we don't rely on bind
# mounts (Coolify path resolution can be brittle, and :ro volumes
# sometimes break SearXNG's runtime state writes in /etc/searxng).
COPY settings.yml /etc/searxng/settings.yml
