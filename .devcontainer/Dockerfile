FROM mcr.microsoft.com/devcontainers/python:1-3.12-bookworm

# The base image's yarn apt repo has an expired signing key, which breaks devcontainer features that run apt-get update.
RUN rm -f /etc/apt/sources.list.d/yarn.list
