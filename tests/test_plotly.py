import sys
print(f"Python version: {sys.version}")
print(f"Python executable: {sys.executable}")

try:
    import plotly
    print(f"Plotly path: {plotly.__file__}")
    print(f"Plotly dir: {dir(plotly)}")
    
    # Try importing plotly.express
    try:
        import plotly.express as px
        print("Successfully imported plotly.express")
        print(f"plotly.express path: {px.__file__}")
    except ImportError as e:
        print(f"Error importing plotly.express: {e}")
    
    # Try importing plotly.graph_objects
    try:
        import plotly.graph_objects as go
        print("Successfully imported plotly.graph_objects")
        print(f"plotly.graph_objects path: {go.__file__}")
    except ImportError as e:
        print(f"Error importing plotly.graph_objects: {e}")
    
except ImportError as e:
    print(f"Import error: {e}")
except Exception as e:
    print(f"Other error: {e}")
    
# Print sys.path to see where Python is looking for modules
print("\nPython path:")
for p in sys.path:
    print(f"  {p}") 