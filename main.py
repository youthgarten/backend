from fastapi import FastAPI

app = FastAPI(title="youthgarten API")


@app.get("/health")
def health():
    return {"status": "ok"}
