# Grading image. Build:  docker build -t quant-comp-runner .
# The worker runs it with: --network none --read-only --cap-drop ALL (+SETUID/SETGID)
# (see worker.docker_cmd). SETUID/SETGID let the entrypoint drop the participant process to
# uid 65534; KILL lets it SIGKILL that process when a tick times out. The entrypoint runs as root only so it can drop the
# participant process to uid 65534; trusted code is root-only (chmod 600) so the
# participant process cannot read bots, the engine or the scenario generator.
FROM python:3.12-slim
RUN pip install --no-cache-dir numpy==2.4.4 pandas==3.0.2
WORKDIR /app
COPY datamodel.py protocol.py sandbox_child.py /app/
COPY engine.py bots.py products.py scenario.py sandbox.py run_job.py /app/
RUN chmod 755 /app \
 && chmod 644 /app/datamodel.py /app/protocol.py /app/sandbox_child.py \
 && chmod 600 /app/engine.py /app/bots.py /app/products.py /app/scenario.py /app/sandbox.py /app/run_job.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0 PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "/app/run_job.py"]
