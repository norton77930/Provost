# When to Mock

Examples use Pester (PowerShell); the principles are framework-agnostic.

Mock at **system boundaries** only:

- External APIs (payment, email, etc.)
- Databases (sometimes - prefer test DB)
- Time/randomness
- File system (sometimes)

Don't mock:

- Your own functions/modules
- Internal collaborators
- Anything you control

## Designing for Mockability

At system boundaries, design interfaces that are easy to mock:

**1. Use dependency injection**

Pass external dependencies in rather than creating them internally:

```powershell
# Easy to mock: the dependency is a parameter
function Invoke-ProcessPayment {
    param($Order, $PaymentClient)
    $PaymentClient.Charge($Order.Total)
}

# Hard to mock: the dependency is created inside
function Invoke-ProcessPayment {
    param($Order)
    $client = [StripeClient]::new($env:STRIPE_KEY)
    $client.Charge($Order.Total)
}
```

**2. Prefer SDK-style interfaces over generic fetchers**

Create specific functions for each external operation instead of one generic function with conditional logic:

```powershell
# GOOD: Each function is independently mockable
function Get-ApiUser   { param($Id)     Invoke-RestMethod "$baseUrl/users/$Id" }
function Get-ApiOrders { param($UserId) Invoke-RestMethod "$baseUrl/users/$UserId/orders" }
function New-ApiOrder  { param($Body)   Invoke-RestMethod "$baseUrl/orders" -Method Post -Body $Body }

# BAD: One generic fetcher — mocking requires conditional logic inside the mock
function Invoke-Api { param($Endpoint, $Options) Invoke-RestMethod $Endpoint @Options }
```

The SDK approach means:

- Each mock returns one specific shape (`Mock Get-ApiUser { @{ Name = 'Alice' } }`)
- No conditional logic in test setup
- Easier to see which endpoints a test exercises

<!-- Adapted from mattpocock/skills (MIT); see skills/NOTICE.md -->
