output "endpoint" {
  description = "Public Azure Search service endpoint, with Entra authorization required."
  value       = "https://${azapi_resource.service.name}.search.windows.net"
}

output "id" {
  description = "Search service ARM ID."
  value       = azapi_resource.service.id
}

output "index_name" {
  description = "Configured index name during foundation; actual managed index name/dependency when enabled. Index creation does not upload documents."
  value       = var.create_index ? azapi_data_plane_resource.index[0].name : var.index_name
}
