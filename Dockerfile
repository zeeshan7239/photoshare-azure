# Step 1: Base image select karein (Python 3.10 slim version sasta aur fast hai)
FROM python:3.10-slim

# Step 2: Working directory set karein
WORKDIR /app

# Step 3: Dependencies install karein
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Step 4: Project ka sara code copy karein
COPY . .

# Step 5: Port 5000 ko open karein (Flask ke liye)
EXPOSE 5000

# Step 6: App ko Gunicorn ke saath run karein (Scalability ke liye dev server se behtar hai)
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "app:app"]