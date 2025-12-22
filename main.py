from fastapi import FastAPI, HTTPException, UploadFile, File, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlmodel import Field, SQLModel, create_engine, Session, select
from typing import List, Optional
import numpy as np
import geopandas as gpd
from shapely.geometry import box, Point, mapping
import uuid
import os
import json
from sklearn.preprocessing import minmax_scale

# ---------------------------
# Database models & setup
# ---------------------------
class TreeReport(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    uuid: str = Field(default_factory=lambda: str(uuid.uuid4()))
    lat: float
    lon: float
    health: str  # e.g., good/fair/poor
    notes: Optional[str] = None

DB_FILE = "uhi_optimizer.db"
engine = create_engine(f"sqlite:///{DB_FILE}", echo=False)

def init_db():
    SQLModel.metadata.create_all(engine)

init_db()

# ---------------------------
# FastAPI app
# ---------------------------
app = FastAPI(title="UHI Greenery Optimizer - Backend")

# ---------------------------
# Helper - synthetic data layers
# ---------------------------
def generate_grid(bbox, grid_size=50):
    """Generate square grid cells (as GeoDataFrame) covering bbox.
    bbox: [minx, miny, maxx, maxy]
    grid_size: number of cells per axis (creates grid_size x grid_size cells)
    """
    minx, miny, maxx, maxy = bbox
    xs = np.linspace(minx, maxx, grid_size + 1)
    ys = np.linspace(miny, maxy, grid_size + 1)
    cells = []
    i = 0
    for xi in range(grid_size):
        for yi in range(grid_size):
            cell = box(xs[xi], ys[yi], xs[xi+1], ys[yi+1])
            cells.append({'geometry': cell, 'cell_id': i})
            i += 1
    gdf = gpd.GeoDataFrame(cells, crs="EPSG:4326")
    return gdf


def simulate_layer_values(gdf, seed=42, scale=1.0, bias=0.0):
    rng = np.random.default_rng(seed)
    n = len(gdf)
    vals = rng.standard_normal(n) * scale + bias
    # normalize 0..1
    vals = minmax_scale(vals)
    return vals

# High-level combined heat score
def compute_heat_score(surface_temp, building_density, traffic):
    # weights can be tuned
    w_temp = 0.6
    w_build = 0.25
    w_traffic = 0.15
    score = w_temp * surface_temp + w_build * building_density + w_traffic * traffic
    # normalize
    return minmax_scale(score)

# Simple optimizer: pick centroids of top-K grid cells with space-aware suppression
def recommend_planting_locations(gdf, heat_scores, k=10, min_distance_m=200):
    gdf = gdf.copy()
    gdf['heat'] = heat_scores
    gdf['centroid'] = gdf.geometry.centroid
    gdf['lon'] = gdf.centroid.x
    gdf['lat'] = gdf.centroid.y
    # sort descending (hot spots first)
    candidates = gdf.sort_values('heat', ascending=False).reset_index(drop=True)

    picks = []
    for idx, row in candidates.iterrows():
        if len(picks) >= k:
            break
        p = Point(row.lon, row.lat)
        too_close = False
        for chosen in picks:
            # quick euclidean (degrees) -> approximate meters by multiplying by 111000
            dist_m = p.distance(Point(chosen['lon'], chosen['lat'])) * 111000
            if dist_m < min_distance_m:
                too_close = True
                break
        if not too_close:
            picks.append({'cell_id': row.cell_id, 'lon': row.lon, 'lat': row.lat, 'score': float(row.heat)})
    return picks

# ---------------------------
# API payload models
# ---------------------------
class BBox(BaseModel):
    minx: float
    miny: float
    maxx: float
    maxy: float
    grid_size: Optional[int] = 50

class HeatmapRequest(BaseModel):
    bbox: BBox
    seed: Optional[int] = 42

class RecommendationRequest(BaseModel):
    bbox: BBox
    top_k: Optional[int] = 10
    min_distance_m: Optional[int] = 200
    seed: Optional[int] = 42

class ReportIn(BaseModel):
    lat: float
    lon: float
    health: str
    notes: Optional[str] = None

# ---------------------------
# Endpoints
# ---------------------------

@app.get("/")
def root():
    return {"message": "UHI Greenery Optimizer Backend is running", "available_endpoints": ["/health", "/generate-heatmap", "/recommendations", "/upload-satellite", "/report-tree", "/reports"]}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/generate-heatmap")
def generate_heatmap(req: HeatmapRequest):
    bbox = [req.bbox.minx, req.bbox.miny, req.bbox.maxx, req.bbox.maxy]
    grid = generate_grid(bbox, grid_size=req.bbox.grid_size)
    # simulate layers
    surface = simulate_layer_values(grid, seed=req.seed, scale=1.5, bias=1.0)
    build = simulate_layer_values(grid, seed=req.seed+1, scale=1.0, bias=0.5)
    traffic = simulate_layer_values(grid, seed=req.seed+2, scale=0.8, bias=0.2)
    heat = compute_heat_score(surface, build, traffic)

    # attach properties and return as GeoJSON FeatureCollection
    features = []
    for idx, row in grid.iterrows():
        props = {
            'cell_id': int(row.cell_id),
            'surface_temp': float(surface[idx]),
            'building_density': float(build[idx]),
            'traffic': float(traffic[idx]),
            'heat_score': float(heat[idx])
        }
        features.append({
            'type': 'Feature',
            'geometry': mapping(row.geometry),
            'properties': props
        })
    fc = {'type': 'FeatureCollection', 'features': features}
    return JSONResponse(content=fc)

@app.post("/recommendations")
def recommendations(req: RecommendationRequest):
    bbox = [req.bbox.minx, req.bbox.miny, req.bbox.maxx, req.bbox.maxy]
    grid = generate_grid(bbox, grid_size=req.bbox.grid_size)
    surface = simulate_layer_values(grid, seed=req.seed, scale=1.5, bias=1.0)
    build = simulate_layer_values(grid, seed=req.seed+1, scale=1.0, bias=0.5)
    traffic = simulate_layer_values(grid, seed=req.seed+2, scale=0.8, bias=0.2)
    heat = compute_heat_score(surface, build, traffic)

    picks = recommend_planting_locations(grid, heat, k=req.top_k, min_distance_m=req.min_distance_m)
    # return as GeoJSON Points
    features = []
    for p in picks:
        pt = Point(p['lon'], p['lat'])
        features.append({
            'type': 'Feature',
            'geometry': mapping(pt),
            'properties': {'cell_id': p['cell_id'], 'score': p['score']}
        })
    return JSONResponse(content={'type': 'FeatureCollection', 'features': features})

@app.post("/upload-satellite")
async def upload_satellite(file: UploadFile = File(...)):
    # placeholder: save file and return path. In production, process raster here.
    dest = os.path.join('uploads', file.filename)
    os.makedirs('uploads', exist_ok=True)
    async with aiofiles.open(dest, 'wb') as out_file:
        content = await file.read()
        await out_file.write(content)
    return {"status": "saved", "path": dest}

@app.post("/report-tree")
def report_tree(report: ReportIn):
    with Session(engine) as session:
        r = TreeReport(lat=report.lat, lon=report.lon, health=report.health, notes=report.notes)
        session.add(r)
        session.commit()
        session.refresh(r)
    return {"status": "ok", "id": r.id, "uuid": r.uuid}

@app.get("/reports")
def get_reports():
    with Session(engine) as session:
        data = session.exec(select(TreeReport)).all()
    arr = []
    for d in data:
        arr.append({'id': d.id, 'uuid': d.uuid, 'lat': d.lat, 'lon': d.lon, 'health': d.health, 'notes': d.notes})
    return {'count': len(arr), 'reports': arr}

# ---------------------------
# Extra utility: run example if called directly
# ---------------------------
if __name__ == '__main__':
    print("Run this with: uvicorn main:app --reload --port 8000")
