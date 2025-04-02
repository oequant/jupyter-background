import plotly.express as px
import pandas as pd
import numpy as np

# Create sample data
x = np.linspace(0, 10, 100)
y = np.sin(x)
df = pd.DataFrame({'x': x, 'y': y})

# Create a plotly express figure
fig = px.line(df, x='x', y='y', title='Sample Plotly Express Figure')

# Print figure info to verify it was created properly
print(f"Figure type: {type(fig)}")
print(f"Figure data: {len(fig.data)} traces")
print(f"Figure layout title: {fig.layout.title.text}")
print(f"Figure layout has xaxis: {'xaxis' in fig.layout}")
print(f"Figure layout has yaxis: {'yaxis' in fig.layout}")

# Save figure as HTML to verify it works
html_file = 'test_figure.html'
fig.write_html(html_file)
print(f"Figure saved to {html_file}")

# Try converting to JSON
json_data = fig.to_json()
print(f"JSON data length: {len(json_data)} characters")
print("Plotly Express figure works correctly!") 