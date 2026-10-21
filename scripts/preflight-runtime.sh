#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' '==== IDENTIDAD DE HOST (solo lectura) ===='
hostnamectl --static
printf '%s\n' '==== RUTAS ===='
ip -4 route show
printf '%s\n' '==== SUBREDES DOCKER EXISTENTES ===='
docker network ls --format 'table {{.Name}}\t{{.Driver}}'
for n in $(docker network ls -q); do
  docker network inspect -f '{{.Name}}: {{range .IPAM.Config}}{{.Subnet}} {{end}}' "$n"
done
printf '%s\n' '==== PORTS LOCALES (5432,6379,8000,8080,9001) ===='
ss -lnt | grep -E ':(5432|6379|8000|8080|9001)[[:space:]]' || true
printf '%s\n' '==== DOCKER Y DISCO ===='
docker --version
docker compose version
df -h /
echo 'Revisar solapamientos de rutas privadas (incl. 172.16/12, 192.168/16) antes de UP.'
