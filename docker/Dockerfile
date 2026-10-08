FROM xiaoql/uv-ffmpeg-image:python3.11-trixie

WORKDIR /workspace

COPY . .

RUN uv sync

ENV SANDPOX_HOST=0.0.0.0 \
    SANDPOX_PORT=7860

EXPOSE 7860

CMD ["uv", "run", "python", "main.py"]