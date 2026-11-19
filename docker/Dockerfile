# OpenFOAM v2512 and XFoil 6.99 in one image (Ubuntu 24.04, arm64 or amd64), so
# the RANS solver and the panel-method reference run in the same environment.
#
#   docker build -t openfoam-xfoil:2512 docker/
#   OF_IMAGE=openfoam-xfoil:2512 .venv/bin/of bash -c 'xfoil < commands.in'
#   .venv/bin/python -m wingstudio.cfd.xfoil --naca 4412 --re 2e6 --alpha 4
#
# Why XFoil is rebuilt: the Ubuntu binary is compiled with
# -fbounds-check -finit-real=inf -ffpe-trap=invalid,zero
# (debian/patches/xfoil-build.diff). With graphics off (PLOP, G F) the plot code
# reads uninitialised reals (inf) and the process dies with SIGFPE at the first
# PPAR or OPER/ALFA; without G F it aborts with "Cannot open display". The stock
# binary works only under Xvfb with xfonts-base installed. Verified on arm64 with
# xfoil 6.99.dfsg+1-3.
#
# This image installs the apt package (runtime libraries, /usr/lib/xfoil/osmap.dat,
# docs, pplot) and replaces /usr/bin/xfoil with the same Ubuntu source package,
# all Debian patches applied, rebuilt without the trap flags. That binary runs
# headless with PLOP / G F and needs no X server. The stock binary is kept as
# /usr/bin/xfoil.ubuntu.

# ---------- stage 1: rebuild XFoil from the Ubuntu source package ----------
FROM opencfd/openfoam-default:2512 AS xfoil-build
USER root
RUN sed -i 's/^Types: deb$/Types: deb deb-src/' /etc/apt/sources.list.d/ubuntu.sources \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        dpkg-dev xz-utils patch gfortran gcc make libx11-dev \
 && mkdir /src && cd /src \
 && apt-get source xfoil \
 && cd xfoil-*/ \
 && sed -i 's/^CHK = .*/CHK = -fallow-argument-mismatch/' bin/Makefile \
 && export FFLAGS=-O2 CFLAGS=-O2 \
 && make -C orrs/bin -f Makefile_DP osgen \
 && (cd orrs && bin/osgen osmaps_ns.lst) \
 && make -C plotlib \
 && make -C bin xfoil \
 && install -m 755 bin/xfoil /xfoil-headless

# ---------- stage 2: OpenFOAM, the apt XFoil and the headless binary ----------
FROM opencfd/openfoam-default:2512
USER root
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends xfoil \
 && rm -rf /var/lib/apt/lists/* \
 && dpkg-divert --add --rename --divert /usr/bin/xfoil.ubuntu /usr/bin/xfoil
COPY --from=xfoil-build /xfoil-headless /usr/bin/xfoil
# The base image sets no USER: `of` passes --user uid:gid itself. The base
# entrypoint (/openfoam/run) is unchanged.
