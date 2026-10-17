# Despliegue por entornos: DEV y PRO

Dos entornos en el mismo servidor, DEV y PRO, cada uno con su dirección:

| Entorno | Para qué | Se actualiza | Dirección de esta app |
|---|---|---|---|
| **DEV** | Probar cualquier cambio en cuanto llega a `main` | Solo, en cada push a `main` | `dev.tudominio.com/tareas/` |
| **PRO** | Probar en producción una versión ya cerrada | Al sacar una versión, tras pulsar **Aprobar** | `pro.tudominio.com/tareas/` |

La misma instancia de PRO puede contestar también en su propio dominio (p. ej.
`tareas.tudominio.com`, en `/`): la app sabe su ruta por la cabecera
`X-Forwarded-Prefix` o por `APP_RUTA_BASE`, y el login vuelve siempre por la
dirección por la que se entró (`APP_URL_PUBLICA` y `APP_URLS_ALTERNATIVAS`).

Todo pasa por git: cada versión es una etiqueta (`v1.0.0`, `v1.1.0`...) y una
imagen Docker con ese mismo nombre en GitHub Packages (`ghcr.io`). Volver atrás
es poner la imagen anterior, que sigue guardada en el servidor.

```
push a main ──► pruebas ──► imagen sha-1a2b3c4 ──► DEV
release v1.2.0 ──► pruebas ──► imagen v1.2.0 ──(Aprobar)──► PRO
"Run workflow" ──► cualquier versión ya construida, o "anterior", al entorno que elijas
pull request ──► solo pruebas
```

El camino de un cambio: se prueba en DEV, se saca como versión y pasa a PRO; si
además hay un cliente con su propia instalación, se le lleva esa misma versión
(ver [Pasar una versión al cliente](#pasar-una-versión-al-cliente)).

Si una versión no arranca, el servidor vuelve solo a la que había.

Lo que hay delante de los entornos (el dominio, la portada con las apps, un
panel para pasar versiones sin terminal, los dominios propios) no es cosa de
esta app: lo pone el servidor o el proxy que tengas. La app solo trae el
workflow (que se puede lanzar desde GitHub o desde cualquier panel que hable
con la API de GitHub) y `tareas.sh`, que despliega en el servidor.

## Qué hay en el repositorio

| Fichero | Qué hace |
|---|---|
| `.github/workflows/despliegue.yml` | Pruebas, imagen y despliegue |
| `.github/actions/desplegar/` | Llama al servidor por SSH |
| `despliegue/servidor/tareas.sh` | Lo del servidor: preparar, desplegar, rollback, copias |
| `despliegue/servidor/docker-compose.yml` | Un entorno; va dentro de la imagen, así cada versión trae el suyo |

## En el servidor

```
/opt/apps/tareas/
  tareas.sh
  bd/            MariaDB compartida (contenedor tareas-bd, sin puertos abiertos)
  dev/ pro/
    .env         secretos y versión actual (no sale del servidor)
    app.env      marca, módulos y login de ese entorno
    historial.log
  copias/<entorno>/   copia de la BD antes de cada despliegue en pro
```

Los dos entornos comparten **una** MariaDB (menos memoria), pero cada uno tiene
su usuario y solo ve sus bases (`app_tareas_dev`, `app_tareas_pro`).
DEV arranca con datos de demostración; PRO, vacío. Memoria máxima por
defecto: 512 MB para DEV, 768 MB para PRO y 1 GB para MariaDB (se cambian
con `APP_MEMORIA` en `<entorno>/.env` y `BD_MEMORIA`/`BD_BUFFER` en `bd/.env`).

## Puesta en marcha (una vez)

### 1. DNS

Dos registros `A` hacia la IP del servidor (en Cloudflare, con la nube en gris,
*DNS only*, para que el proxy pueda sacar el certificado). Si el servidor ya
los tiene para otras apps, no hace falta nada:

| Tipo | Nombre | Valor |
|---|---|---|
| A | `dev` | IP del servidor |
| A | `pro` | IP del servidor |

### 2. Servidor

Con el usuario que vaya a desplegar (root, o uno del grupo `docker`):

```bash
mkdir -p /opt/apps/tareas && cd /opt/apps/tareas
curl -fsSLO https://raw.githubusercontent.com/<usuario>/<repo>/main/despliegue/servidor/tareas.sh
chmod 700 tareas.sh
./tareas.sh preparar ghcr.io/<usuario>/<repo>
```

`preparar` (se puede repetir sin miedo, no pisa nada):

- crea las carpetas y genera **en el servidor** todas las contraseñas y claves;
- levanta la MariaDB compartida y crea un usuario por entorno;
- crea la clave SSH con la que GitHub entra, **limitada** a `tareas.sh ci`
  (solo puede desplegar, hacer rollback y consultar; nunca abrir una shell);
- te enseña qué copiar a GitHub.

Si la red del proxy no se llama `proxy_default`: `RED_PROXY=<red> ./tareas.sh preparar ...`.

### 3. GitHub

En el repositorio → *Settings*:

**Secrets and variables → Actions → Secrets**

| Secreto | Valor |
|---|---|
| `DEPLOY_SSH_KEY` | `cat /opt/apps/tareas/.ssh/github_despliegue` (la clave privada entera) |
| `DEPLOY_HOST` | IP o nombre del servidor |
| `DEPLOY_KNOWN_HOSTS` | La línea que imprime `preparar` (o `ssh-keyscan -t ed25519 <servidor>` desde tu PC) |

**Variables** (opcionales): `DEPLOY_USER` (por defecto `root`), `DEPLOY_PORT`
(por defecto `22`), y `URL_DEV` y `URL_PRO` para que GitHub enseñe el
enlace de cada entorno.

**Environments**: `dev` y `pro`, con *Required reviewers* en **pro** (tú)
para que nada llegue a producción sin tu aprobación: *Settings →
Environments → New environment*.

Mientras falten los secretos, las pruebas y las imágenes se hacen igual y el
paso de desplegar solo avisa.

### 4. La dirección de cada entorno

Cada entorno es el contenedor `tareas-<entorno>` (puerto `8000`) en la red del
proxy. Dos formas de llegar a él:

- **Con una entrada por rutas** (un nginx del servidor que reparte
  `pro.tudominio.com/<app>/` entre varias apps): que mande `/tareas/` a
  `http://tareas-pro:8000` con `X-Forwarded-Prefix: /tareas`, sin quitar la
  ruta, y con `Upgrade`/`Connection` para los websockets.
- **Solo esta app**: en Nginx Proxy Manager, un *Proxy Host* por entorno
  (`dev.tudominio.com` → `tareas-dev`, `pro.tudominio.com` → `tareas-pro`,
  puerto `8000`), con *Block Common Exploits*, *Websockets Support*, *Request
  a new SSL Certificate* + *Force SSL* y, en *Advanced*,
  `client_max_body_size 50m;`. La app queda en la raíz del dominio.

### 5. Primer despliegue

Haz un push a `main` (o *Actions → Despliegue → Run workflow*, `dev`): DEV queda
en marcha. Entra con `admin` y la contraseña de
`grep INSTALAR_ADMIN_PASSWORD /opt/apps/tareas/*/.env`, y cámbiala.

## Versiones y vuelta atrás

Desde GitHub o desde el servidor.

**Probar un cambio**: push (o merge de un PR) a `main` → en unos minutos está en DEV.

**Sacar una versión**: GitHub → *Releases* → *Draft a new release* → *Choose a tag*,
escribe la nueva (`v1.3.0`) → *Generate release notes* → *Publish release*.
Se construye y la ejecución queda esperando en *Actions*: *Review deployments* →
`pro` → *Approve and deploy*.

Numeración: `v<grande>.<mediano>.<pequeño>`; sube el último para arreglos, el
del medio para funciones nuevas y el primero para cambios que obligan a algo
(migraciones, configuración nueva).

**Volver atrás (rollback)**:

- Desde GitHub: *Actions → Despliegue → Run workflow*, entorno y versión
  `anterior` (o una concreta, `v1.2.0`). En `pro` también pide aprobación.
- Desde el servidor (lo más rápido si hay prisa, y funciona aunque la app no):
  ```bash
  /opt/apps/tareas/tareas.sh rollback pro            # a la versión buena anterior
  /opt/apps/tareas/tareas.sh rollback pro v1.1.0     # a una concreta
  ```

**Otras órdenes** (en `/opt/apps/tareas`):

```bash
./tareas.sh estado                  # versión y salud de cada entorno
./tareas.sh historial pro           # qué se desplegó y cuándo
./tareas.sh logs pro                # log de la app
./tareas.sh copia pro               # copia de la BD ahora
./tareas.sh restaurar pro FICHERO   # volver la BD a una copia (hace otra antes)
./tareas.sh pro-a-dev               # copia los datos de PRO en DEV para probar con datos reales
./tareas.sh importar pro ORIGEN     # datos de una instalación de antes (ver más abajo)
```

**Cambiar la configuración** de un entorno (marca, login, módulos): edita
`<entorno>/app.env` y vuelve a desplegar su versión actual
(`./tareas.sh desplegar pro $(grep ^VERSION= pro/.env | cut -d= -f2)`).

## Login con Keycloak (todas las apps)

Para que los usuarios y sus permisos se gestionen en Keycloak y una sola
sesión valga para todas las apps del realm (`AUTH_MODO=sso`):

1. **Grupos** (Groups), uno por app con subgrupos:

   | Grupo | Qué da |
   |---|---|
   | `IT` | Entra en todas las apps y las administra |
   | `/Tareas` | Entra en el Gestor de Tareas |
   | `/Tareas/Admin` | Administra solo el Gestor de Tareas |
   | `/Tareas/<Departamento>` | Entra (para separar por departamento) |

   Quien no esté en `/Tareas` (ni en un subgrupo, ni en `IT`) no puede entrar,
   aunque sea admin de otra app.

2. **Cliente** por entorno (Clients → *Create client*), p. ej. `tareas-dev`:
   *Client authentication* activado, *Standard flow* activado,
   *Valid redirect URIs* = `https://dev.<dominio>/tareas/login/sso/`,
   *Valid post logout redirect URIs* = `https://dev.<dominio>/*`.
   En *Client scopes* → `tareas-dev-dedicated` → *Add mapper* → *By
   configuration* → *Group Membership*: *Token Claim Name* `groups`,
   **Full group path activado**, *Add to userinfo* activado.
   Copia el secreto de *Credentials*.

3. **En el servidor**, con la versión que trae este cambio ya desplegada:

   ```bash
   ./tareas.sh keycloak dev https://dev.<dominio>/tareas https://<keycloak>/auth/realms/<realm>
   ```

   Pide el client secret (no se ve al escribirlo), lo guarda en `dev/app.env`
   y vuelve a arrancar el entorno. Igual con `pro` y su cliente `tareas-pro`.
   Otro nombre de grupo: `GRUPO_APP=Otro ./tareas.sh keycloak ...`.

Con `sso`, el usuario `admin` local deja de servir: se entra siempre por
Keycloak y la primera vez se da de alta sola la persona en la app.

## Datos de una instalación de antes

Cuando ya había un Gestor de Tareas suelto con sus datos, `importar` los trae
a un entorno (antes guarda copia de lo que hay):

```bash
./tareas.sh importar pro <contenedor-de-su-bd> <contenedor-de-su-app>
```

Acepta también un volcado (`.sql` o `.sql.gz`) en vez del contenedor; si en el
contenedor hay más de una base, se elige con `BD_ORIGEN=<base> ./tareas.sh importar ...`.
Con el contenedor de la app se traen también los adjuntos.

## Pasar una versión al cliente

La instalación de un cliente vive en su propio repositorio, que parte de este
y solo añade su configuración. Llevarle una versión
ya probada en tu PRO es traer esa etiqueta y desplegar allí:

```bash
# en el repositorio del cliente, una vez:
git remote add base https://github.com/<usuario>/<este-repo>.git
# cada vez:
git fetch base --tags && git merge v1.3.0 && git push
```

y en su servidor, lo que use esa instalación (`git pull && docker compose up -d --build`,
o este mismo sistema si el cliente también lo tiene).

## Base de datos y rollback

Volver a la imagen anterior no deshace los cambios en la base de datos. Por eso:

- antes de cada despliegue en PRO se guarda una copia en `copias/<entorno>/`
  (se quedan las 15 últimas);
- una versión que cambie el esquema (un `scripts/migrar_<algo>.py`) debe poder
  convivir con la versión anterior, o se acompaña de `restaurar` al volver atrás:
  ```bash
  ./tareas.sh rollback pro v1.1.0
  ./tareas.sh restaurar pro 20261016-224254_antes-de-v1.2.0.sql.gz
  ```
- las migraciones se lanzan a mano tras desplegar:
  `docker exec tareas-pro-app-1 python scripts/migrar_<algo>.py`. Pruébalas antes
  en DEV con datos de PRO (`pro-a-dev`).

## Imágenes

Cada push a `main` crea `sha-<commit>` y `main`; cada release, `v<x.y.z>` y
`latest`. El servidor guarda las de las 4 últimas versiones buenas de cada
entorno (para volver sin descargar nada) y borra las demás.

Si el paquete de `ghcr.io` es privado, el servidor solo puede descargar durante
los despliegues lanzados desde GitHub (reciben un permiso temporal). Para
desplegar desde el servidor una versión que aún no tiene, haz público el paquete
(*Packages* → el paquete → *Package settings* → *Change visibility*) o entra una
vez con `docker login ghcr.io` y un token con permiso `read:packages`.
