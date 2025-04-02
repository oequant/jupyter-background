from setuptools import setup, find_packages

setup(
    name='background_magic',
    version='0.1.0',
    description='Jupyter magic to run cells in the background',
    author='Your Name', # Replace with your name/organisation
    author_email='your.email@example.com', # Replace with your email
    packages=find_packages(),
    install_requires=[
        'ipython>=7.0', # Dependency for magics
        'cloudpickle', # For serializing execution context
    ],
    extras_require={
        'test': [
            'pandas', # For DataFrame testing
            'numpy',  # Dependency for pandas and testing
            'matplotlib', # For testing display hooks
            'Pillow',     # Dependency for matplotlib
            'pytest',
            'pytest-asyncio',
        ]
    },
    classifiers=[
        'Framework :: IPython',
        'Programming Language :: Python :: 3',
        'License :: OSI Approved :: MIT License', # Choose appropriate license
        'Operating System :: OS Independent',
    ],
    python_requires='>=3.7', # Specify compatible Python versions
) 