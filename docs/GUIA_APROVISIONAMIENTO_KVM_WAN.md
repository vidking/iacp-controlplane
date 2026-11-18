# Guía de aprovisionamiento puntual de una KVM por WAN en Dev

Revisión: 18 de noviembre de 2026  
Destino: `dev-iac.cyberfuel.com` → entorno lógico `lab` → clúster MNG  
Tipo de prueba: canaria puntual; no equivale a certificación física integral.

## 1. Propósito y alcance

Esta guía crea una KVM de prueba en el entorno Dev usando la red WAN estática administrada por IPAM. El perfil correcto es `prod_static`; `lab_dhcp` aprovisiona por la LAN de laboratorio y no sirve para validar WAN.

La solicitud crea recursos reales: una KVM en el clúster MNG y una reserva de IP en NetBox. Los pasos de esta guía no eliminan la KVM ni liberan su dirección. La limpieza es una operación separada que requiere autorización explícita.

El destino se expresa como `environment: "lab"`. El IACP Control Plane (VM 104) resuelve el nodo y la configuración física. No se debe seleccionar el nodo en el request.

## 2. Contrato WAN verificado

La consulta de solo lectura a `GET /v1/network-profiles` en Dev, compartida por el equipo de operaciones el 18 de noviembre de 2026, mostró:

- `profile_id`: `prod_static`
- `enabled`: `true`
- `proxmox_bridge`: `vmbr3`
- `ipam`: `netbox`
- `prefix`: `190.0.231.0/24`
- `gateway`: `190.0.231.1`

Antes de cada ejecución vuelve a consultar el endpoint. La configuración puede cambiar. NetBox asigna la IP disponible; no elijas ni envíes una IP manualmente. `190.0.231.2` está excluida de los ensayos por la discrepancia ARP documentada y no debe usarse aunque NetBox la muestre disponible.

No agregues al request `ipv4_cidr`, gateway, VLAN, `vmbr3`, VMID, nodo ni parámetros de Proxmox. El servidor debe resolverlos a partir de `prod_static`.

## 3. Marcadores que debes sustituir

- `PLAN_ID` e `IMAGE_ID`: valores publicados por los catálogos de Dev. El ejemplo usa `kvm-2` y `ubuntu-2404`, confirmados en la respuesta compartida.
- `SSH_PUBLIC_KEY_FILE`: ruta de una clave pública exclusiva para el ensayo. No uses ni copies una clave privada.
- `RUN_ID`: lo genera `date` en UTC; da un nombre único a la KVM y a la clave de idempotencia.
- Token API y contraseña root: se solicitan sin eco en la terminal. No los pegues en tickets, logs ni archivos del repositorio.

No habilites `set -x`. No escribas los valores de secretos en la línea de comandos.

## 4. Preflight de solo lectura

Ejecuta en Bash desde una estación autorizada. Estos comandos consultan Dev; todavía no crean recursos:

```bash
set -euo pipefail
export IACP_BASE_URL='https://dev-iac.cyberfuel.com'
read -r -s -p 'Token API de Dev: ' IACP_API_TOKEN
printf '\n'
AUTH=(-H "Authorization: Bearer ${IACP_API_TOKEN}")

PROFILE_JSON=$(curl --fail-with-body -sS "${AUTH[@]}" \
  "${IACP_BASE_URL}/v1/network-profiles")
printf '%s\n' "$PROFILE_JSON" | jq '.network_profiles[] | select(.profile_id == "prod_static")'
if ! printf '%s\n' "$PROFILE_JSON" | jq -e '
  any(.network_profiles[];
    .profile_id == "prod_static" and .enabled == true and
    .proxmox_bridge == "vmbr3" and .ipam == "netbox" and
    .prefix == "190.0.231.0/24" and .gateway == "190.0.231.1")
' >/dev/null; then
  printf '%s\n' 'BLOCKED: el perfil WAN no coincide con la configuración autorizada.' >&2
  exit 1
fi

curl --fail-with-body -sS "${AUTH[@]}" \
  "${IACP_BASE_URL}/v1/images" | jq '.images'
curl --fail-with-body -sS "${AUTH[@]}" \
  "${IACP_BASE_URL}/v1/plans" | jq '.plans'

OBS_JSON=$(curl --fail-with-body -sS "${AUTH[@]}" \
  "${IACP_BASE_URL}/v1/observability")
if ! printf '%s\n' "$OBS_JSON" | jq -e '
  .max_concurrent == 6 and .active_operations == 0 and
  .queue_depth == 0 and .uncertain_operations == 0 and
  (.workers | length) == 6 and
  all(.workers[];
    .status == "idle" and .liveness == "healthy" and
    (.current_operation_id == null or .current_operation_id == ""))
' >/dev/null; then
  printf '%s\n' 'BLOCKED: no hay seis workers de infraestructura sanos e idle.' >&2
  exit 1
fi
printf '%s\n' 'Preflight de perfil WAN y workers de infraestructura: PASS'
```

Detén el ensayo si falla el token, el perfil, cualquiera de los seis workers, la cola o las operaciones inciertas. No reduzcas la concurrencia de la matriz autorizada ni intervengas workers compartidos. El preflight de esta guía no sustituye la revisión del estado de operaciones, versión de runtime e aislamiento exigida para una matriz física.

## 5. Preparar el request WAN

El ejemplo crea una KVM Ubuntu 24.04 con el plan `kvm-2`. Cambia esos dos identificadores solo por valores que aparezcan en los catálogos recién consultados. Define la ruta de una clave pública de ensayo existente.

```bash
PLAN_ID='kvm-2'
IMAGE_ID='ubuntu-2404'
SSH_PUBLIC_KEY_FILE="$HOME/.ssh/iacp-wan-test.pub"
test -r "$SSH_PUBLIC_KEY_FILE" || {
  printf 'No se puede leer la clave pública: %s\n' "$SSH_PUBLIC_KEY_FILE" >&2
  exit 1
}
SSH_PUBLIC_KEY=$(<"$SSH_PUBLIC_KEY_FILE")
RUN_ID=$(date -u +%Y%m%d%H%M%S)
VM_NAME="prueba-kvm-wan-${RUN_ID}"
IDEMPOTENCY_KEY="crear-kvm-wan-${RUN_ID}"
read -r -s -p 'Contraseña root temporal (mínimo 12 caracteres): ' IACP_ROOT_PASSWORD
printf '\n'

umask 077
REQUEST_FILE=$(mktemp "${TMPDIR:-/tmp}/iacp-kvm-wan.XXXXXX")
RESPONSE_FILE=$(mktemp "${TMPDIR:-/tmp}/iacp-kvm-wan-response.XXXXXX")
trap 'rm -f -- "$REQUEST_FILE" "$RESPONSE_FILE"' EXIT
printf '%s' "$IACP_ROOT_PASSWORD" | jq -n \
  --arg name "$VM_NAME" \
  --arg plan_id "$PLAN_ID" \
  --arg image_id "$IMAGE_ID" \
  --arg ssh_public_key "$SSH_PUBLIC_KEY" \
  --arg tag "wan-smoke-${RUN_ID}" \
  --rawfile root_password /dev/stdin \
  '{
    name: $name,
    plan_id: $plan_id,
    image_id: $image_id,
    environment: "lab",
    network_profile: "prod_static",
    access: {
      root_password: $root_password,
      ssh_public_keys: [$ssh_public_key]
    },
    tags: [$tag],
    start_on_create: true
  }' > "$REQUEST_FILE"
chmod 600 "$REQUEST_FILE"
unset IACP_ROOT_PASSWORD
```

Confirma que el request usa exactamente `network_profile: "prod_static"`. No reutilices un nombre ni una `Idempotency-Key` de otra prueba.

## 6. Crear y seguir la operación

El siguiente `POST` es el primer paso que cambia estado. Registra los IDs devueltos sin registrar contraseñas:

```bash
HTTP_CODE=$(curl -sS -o "$RESPONSE_FILE" -w '%{http_code}' \
  -X POST "${IACP_BASE_URL}/v1/instances" \
  "${AUTH[@]}" \
  -H "Idempotency-Key: ${IDEMPOTENCY_KEY}" \
  -H 'Content-Type: application/json' \
  --data-binary "@${REQUEST_FILE}")
if [ "$HTTP_CODE" != '202' ]; then
  printf 'POST no aceptado: HTTP %s. No generes otra clave ni otra KVM.\n' "$HTTP_CODE" >&2
  exit 1
fi
RESPONSE=$(<"$RESPONSE_FILE")
printf '%s\n' "$RESPONSE" | jq '{operation_id, instance_id, status, idempotency_key}'
OPERATION_ID=$(jq -er '.operation_id' <<<"$RESPONSE")
INSTANCE_ID=$(jq -er '.instance_id' <<<"$RESPONSE")
```

Consulta la operación cada 10 segundos, hasta 30 minutos. Si termina en `failed`, `uncertain`, un estado desconocido o vence el plazo, detén nuevas inyecciones y pide recuperación controlada. No repitas el `POST` con otra clave ni liberes la IP manualmente.

```bash
for attempt in $(seq 1 180); do
  OPERATION=$(curl --fail-with-body -sS "${AUTH[@]}" \
    "${IACP_BASE_URL}/v1/operations/${OPERATION_ID}")
  STATUS=$(jq -r '.status' <<<"$OPERATION")
  printf 'Operación %s: %s\n' "$OPERATION_ID" "$STATUS"
  case "$STATUS" in
    queued|running) sleep 10 ;;
    completed|succeeded) break ;;
    failed|uncertain)
      printf '%s\n' "$OPERATION" | jq '{operation_id, status, internal_status}'
      exit 1
      ;;
    *) printf 'BLOCKED: estado no reconocido: %s\n' "$STATUS" >&2; exit 1 ;;
  esac
done
if [ "$STATUS" != 'completed' ] && [ "$STATUS" != 'succeeded' ]; then
  printf '%s\n' 'BLOCKED: venció el plazo; conserva la intención y solicita recuperación.' >&2
  exit 1
fi
```

Si el `POST` termina por timeout o pierde la respuesta, su resultado es desconocido. No crees otra KVM: consulta primero las operaciones y el inventario por el nombre `VM_NAME`, y solicita reconciliación si el resultado físico no está claro.

## 7. Verificar la asignación WAN

La aceptación HTTP no prueba conectividad. Consulta el estado de la KVM y la observación del QEMU Guest Agent:

```bash
curl --fail-with-body -sS "${AUTH[@]}" \
  "${IACP_BASE_URL}/v1/instances/${INSTANCE_ID}" |
  jq '{instance_id, name, vmid, node, status, spec: {network_profile: .spec.network_profile}}'

NETWORK_JSON=$(curl --fail-with-body -sS "${AUTH[@]}" \
  "${IACP_BASE_URL}/v1/instances/${INSTANCE_ID}/network")
printf '%s\n' "$NETWORK_JSON" |
  jq '{network_profile, configured_ipv4_cidr, configured_ipv4_gateway,
       observed_ipv4, status, network_allocation}'
printf '%s\n' "$NETWORK_JSON" | jq -e '
  (.network_allocation.address | split("/")[0]) as $ip |
  (.network_profile == "prod_static" and
   .network_allocation.status == "allocated" and
   .network_allocation.gateway == "190.0.231.1" and
   ($ip | startswith("190.0.231.")) and $ip != "190.0.231.1" and
   $ip != "190.0.231.2")
' >/dev/null
```

Comprueba además que la dirección observada pertenece a la KVM correcta, que la ruta por defecto usa `190.0.231.1` y que el tráfico WAN funciona desde el punto de prueba autorizado. Una reserva IPAM, una ruta configurada o una URL no prueban por sí solas conectividad de capa 2/3. Si falla el vecino del gateway, SSH o la salida WAN, registra `FAIL` o `BLOCKED`, conserva la KVM y la asignación, y detén nuevas inyecciones.

## 8. Resultado y límites

Registra fecha UTC, URL de Dev, revisión realmente servida, `RUN_ID`, IDs de operación/instancia, perfil, VMID, nodo, dirección asignada y evidencia de conectividad. Nunca registres el token, la contraseña root ni secretos.

Esta solicitud individual es una prueba de humo, no la matriz de certificación WAN. La matriz física aprobada usa seis aprovisionamientos simultáneos, revisa el pool autorizado, el bridge y la capacidad, y ejecuta limpieza de sus propias canarias. Esa limpieza elimina recursos y libera direcciones; requiere autorización explícita y no forma parte de esta guía. No ejecutes producción.
