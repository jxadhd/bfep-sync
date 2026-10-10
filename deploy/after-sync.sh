#!/bin/sh
# Runs after a sync run that changed figures (the --after hook). Invalidates
# the website's cached pages that show raised/goal amounts, by cache tag.
# This is much lighter than `drush cr`, which rebuilds every cache.
#
# BFEP_DRUSH is how drush is called on this server, for example
#   BFEP_DRUSH="drush -r /var/www/bfep/web"
#   BFEP_DRUSH="docker exec bfep-drupal drush"
# Set it in /etc/bfep-sync.env. It defaults to plain `drush`.
set -eu
DRUSH="${BFEP_DRUSH:-drush}"
# Word splitting of $DRUSH is intended, so it can hold a whole command.
# shellcheck disable=SC2086
exec $DRUSH cache:tags bfep:campaigns,bfep:countries,bfep:home
