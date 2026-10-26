#!/bin/sh

set -eu
cd "$(dirname "$0")"

DEV="${GOPRO_DEV:-/dev/video10}"
NR="${DEV#/dev/video}"
LABEL="GoPro Session"

PATRON="python3 .*gopro_session_webcam.py"
RESET=0
if [ "${1:-}" = "--reset" ]; then
    RESET=1
    shift
    "./$(basename "$0")" --stop
fi
if [ "${1:-}" = "--stop" ]; then
    if pkill -TERM -f "$PATRON"; then
        echo "Parando la webcam de la GoPro..."
        i=0
        while pgrep -f "$PATRON" >/dev/null 2>&1 && [ "$i" -lt 20 ]; do sleep 0.25; i=$((i + 1)); done
        if pgrep -f "$PATRON" >/dev/null 2>&1; then
            echo "Sigue activa tras 5 s: la fuerzo."
            pkill -KILL -f "$PATRON" || true
        fi
        echo "Parada."
    else
        echo "La webcam no estaba en marcha."
    fi
    exit 0
fi

need() {
    command -v "$1" >/dev/null 2>&1 || { echo "Falta '$1' (sudo apt install $2)"; exit 1; }
}
need ffmpeg ffmpeg
need python3 python3

if [ -t 0 ]; then SUDO="sudo"; else SUDO="sudo -n"; fi

if [ "${1:-}" = "--install-module" ]; then
    existing="$(grep -rls v4l2loopback /etc/modprobe.d /etc/modules-load.d /etc/modules 2>/dev/null || true)"
    if [ -n "$existing" ]; then
        echo "Ya hay configuracion de v4l2loopback en:"
        echo "$existing"
        echo "Revisala a mano para no duplicar 'options' (necesitas: devices=1 video_nr=$NR"
        echo "card_label=\"$LABEL\" exclusive_caps=1) y reinicia o recarga el modulo."
        exit 1
    fi
    echo "options v4l2loopback devices=1 video_nr=$NR card_label=\"$LABEL\" exclusive_caps=1" \
        | $SUDO tee /etc/modprobe.d/gopro-v4l2loopback.conf >/dev/null
    echo v4l2loopback | $SUDO tee /etc/modules-load.d/gopro-v4l2loopback.conf >/dev/null
    echo "Hecho. En el proximo arranque existira $DEV sin necesidad de sudo."
    exit 0
fi

dev_name() { cat "/sys/class/video4linux/video$NR/name" 2>/dev/null || true; }

if [ "$RESET" = 1 ] && lsmod | grep -q '^v4l2loopback'; then
    echo "Recargando v4l2loopback para desatascar $DEV..."
    $SUDO rmmod v4l2loopback || {
        echo "No se pudo descargar: alguna app tiene abierta la webcam virtual."
        echo "Cierra Discord/OBS/navegador (o su videollamada) y repite --reset. Quien la usa:"
        fuser -v /dev/video* 2>&1 | grep -v "^\s*$" || true
        exit 1
    }
fi

if [ -e "$DEV" ] && [ "$(dev_name)" != "$LABEL" ]; then
    echo "$DEV existe pero es otro dispositivo ('$(dev_name)'), no la webcam virtual."
    echo "Usa otro numero:  GOPRO_DEV=/dev/video11 $0   (y recarga el modulo con ese video_nr)"
    exit 1
fi

if [ ! -e "$DEV" ]; then
    if lsmod | grep -q '^v4l2loopback'; then
        echo "v4l2loopback ya estaba cargado, pero sin $DEV (otros parametros)."
        echo "Lo descargo y lo vuelvo a cargar con video_nr=$NR..."
        $SUDO rmmod v4l2loopback || {
            echo "No se pudo descargar: algo usa un /dev/videoN de v4l2loopback"
            echo "(cierra OBS/navegador/ffplay e intentalo de nuevo)."
            exit 1
        }
    fi
    echo "Cargando v4l2loopback (video_nr=$NR)..."
    $SUDO modprobe v4l2loopback devices=1 video_nr="$NR" card_label="$LABEL" exclusive_caps=1
fi

if [ ! -e "$DEV" ]; then
    echo "Sigue sin existir $DEV tras cargar el modulo."
    echo "Comprueba 'dkms status': puede que v4l2loopback no se haya compilado"
    echo "para el kernel actual (reinstala o reinicia tras instalarlo)."
    exit 1
fi

exec python3 gopro_session_webcam.py --device "$DEV" "$@"
