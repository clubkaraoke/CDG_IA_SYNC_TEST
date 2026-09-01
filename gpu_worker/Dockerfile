FROM pytorch/pytorch:2.8.0-cuda12.8-cudnn9-runtime

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/models/hf

WORKDIR /worker

RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg sox libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

COPY gpu_worker/requirements.txt /worker/requirements.txt
RUN pip install -r /worker/requirements.txt

COPY gpu_worker/app.py /worker/app.py

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
