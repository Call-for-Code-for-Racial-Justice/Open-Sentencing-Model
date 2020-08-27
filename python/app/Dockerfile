FROM tiangolo/meinheld-gunicorn-flask:python3.7

# install dependencies
RUN pip install --no-cache-dir --upgrade pip

# copy requirements.txt
COPY ./requirements.txt /app/requirements.txt

# install project requirements
RUN pip install --no-cache-dir -r requirements.txt

COPY ./app /app
