# Vomeet Documentation

This directory contains comprehensive documentation for Vomeet self-hosted deployments.

## Available Documentation

### Setup & Deployment

- **[Deployment Guide](deployment.md)** - Complete setup and deployment instructions for self-hosted Vomeet

### User Guides

- **[Self-Hosted Management Guide](self-hosted-management.md)** - Complete guide for managing users and API tokens in self-hosted Vomeet deployments
  - User creation and management
  - API token generation and revocation
  - Complete workflow examples (curl + Python)
  - Quick reference and troubleshooting

- **[WebSocket Guide](websocket.md)** - Real-time transcript streaming via WebSocket
  
- **[User API Guide](user_api_guide.md)** - Complete REST API reference

## Example Notebooks

Interactive Jupyter notebooks for testing and development are located in the `../nbs/` directory:

- `0_basic_test.ipynb` - Complete bot lifecycle test
- `1_load_tests.ipynb` - Load testing with multiple users
- `2_bot_concurrency.ipynb` - Concurrent bot testing
- `3_API_validation.ipynb` - API endpoint validation
- `manage_users.ipynb` - User management examples

## Getting Started

1. **Deploy Vomeet**: Follow the [Deployment Guide](deployment.md) to get Vomeet running
2. **Manage Users**: Read the [Self-Hosted Management Guide](self-hosted-management.md)
3. **Use the API**: See [User API Guide](user_api_guide.md) and [WebSocket Guide](websocket.md)

## Support

- **GitHub Issues**: https://github.com/voltade/vomeet/issues
- **Website**: https://vomeet.ai

