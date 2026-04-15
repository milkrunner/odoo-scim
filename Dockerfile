FROM python:3.12-bookworm

# System dependencies for Odoo
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libldap2-dev \
    libsasl2-dev \
    libpq-dev \
    libxml2-dev \
    libxslt1-dev \
    libjpeg62-turbo-dev \
    zlib1g-dev \
    libfreetype6-dev \
    liblcms2-dev \
    libmagic1 \
    wkhtmltopdf \
    node-less \
    npm \
    && rm -rf /var/lib/apt/lists/*

# Install rtlcss for Odoo
RUN npm install -g rtlcss

WORKDIR /opt/odoo

# Install Python dependencies
COPY requirements.txt /opt/odoo/
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . /opt/odoo/

EXPOSE 8069 8072

ENTRYPOINT ["python3", "odoo-bin"]
CMD ["-c", "/etc/odoo/odoo.conf"]
