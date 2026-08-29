FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PHECODEX_HOME=/opt/phecodex

WORKDIR ${PHECODEX_HOME}

COPY requirements-lock.txt pyproject.toml README.md ANALYST_GUIDE.md ./
COPY src ./src
COPY examples ./examples
COPY scripts/verify_release.py scripts/prepare_ukb_for_mapping.R scripts/plot_phecode_attrition.py scripts/package_distribution.py scripts/check_prevalence.py scripts/check_deidentification.py scripts/reconcile_attrition.py scripts/deidentify_ukb_for_testing.R ./scripts/

RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements-lock.txt \
    && python -m pip install --no-cache-dir . \
    && python -m pip cache purge

ENTRYPOINT ["phecodex-map"]
CMD ["--help"]
